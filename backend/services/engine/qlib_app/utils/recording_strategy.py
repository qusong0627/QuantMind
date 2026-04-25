import json
import logging

import pandas as pd
from qlib.backtest.decision import Order, OrderDir
from qlib.contrib.strategy.signal_strategy import (
    TopkDropoutStrategy,
    WeightStrategyBase,
)

import redis

from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

logger = logging.getLogger(__name__)

# 所有「本项目自定义 / 前端传入」的 kwargs，Qlib 的 BaseStrategy 不认识它们，
# 必须在调用 super().__init__() 之前统一 pop。
_OUR_KWARGS = {
    # Redis 连接
    "backtest_id",
    "redis_host",
    "redis_port",
    "redis_db",
    "redis_password",
    # 动态风險 / 市场状态
    "market_state_series",
    "position_by_state",
    "strategy_total_position",
    "risk_degree",
    "dynamic_position",
    "market_state_symbol",
    # 前端费率配置（CnExchange 处理，不属于策略层）
    "buy_cost",
    "sell_cost",
    # 历史遗留字段（前端 QlibStrategyParams 曾存在的额外字段）
    "drop_thresh",
    # 股票池文件路径（由平台在上层消费，不传给 qlib BaseStrategy）
    "pool_file",
    # 融资融券相关字段
    "financing_rate",
    "borrow_rate",
    "max_short_exposure",
    "max_leverage",
    "account_stop_loss",
}


def _normalize_display_quantity(symbol: str, quantity: float) -> int:
    """A 股展示数量纠偏，避免复权因子日间漂移造成非整手抖动。"""
    qty_int = int(round(float(quantity)))
    symbol_upper = str(symbol or "").upper()
    if symbol_upper.startswith(("SH", "SZ", "BJ")) and qty_int >= 100:
        lot_rounded = int(round(qty_int / 100.0) * 100)
        if abs(qty_int - lot_rounded) <= 2:
            return lot_rounded
    return qty_int


class RedisLoggerMixin:
    """
    Redis 交易记录与进度追踪混入类
    """

    def init_redis(self, kwargs):
        self.backtest_id = kwargs.pop("backtest_id", None)
        log = StructuredTaskLogger(
            logger,
            "redis-logger-mixin",
            {"backtest_id": self.backtest_id, "strategy": self.__class__.__name__},
        )

        # 优先使用项目中统一的 Redis 哨兵客户端获取方式
        try:
            from backend.shared.redis_sentinel_client import get_redis_sentinel_client

            self.redis_client = get_redis_sentinel_client()
            self.redis_client._ensure_connection()
            log.info("redis_init", "RedisLogger initialized via Sentinel client")
        except Exception as e:
            log.warning("redis_sentinel_unavailable", "无法使用哨兵客户端，尝试传统连接", error=e)
            self.redis_host = kwargs.pop("redis_host", "localhost")
            self.redis_port = kwargs.pop("redis_port", 6379)
            self.redis_db = kwargs.pop("redis_db", 0)
            self.redis_password = kwargs.pop("redis_password", None)

            self.redis_client = None
            if self.backtest_id:
                try:
                    self.redis_client = redis.Redis(
                        host=self.redis_host,
                        port=self.redis_port,
                        db=self.redis_db,
                        password=self.redis_password,
                        decode_responses=True,
                    )
                except Exception as e:
                    log.error("redis_connect_failed", "Redis连接失败", error=e)

    def log_progress(self):
        """记录回测进度"""
        if not self.redis_client or not self.backtest_id:
            return

        try:
            # 获取当前步长和日历
            trade_step = getattr(self, "trade_step", 0)
            trade_calendar = getattr(self, "trade_calendar", [])

            if not trade_calendar:
                # 尝试从 exchange 获取
                exchange = getattr(self, "trade_exchange", None)
                if exchange and hasattr(exchange, "trade_calendar"):
                    trade_calendar = exchange.trade_calendar

            if len(trade_calendar) > 0:
                progress = min(1.0, float(trade_step) / len(trade_calendar))
                if hasattr(trade_calendar, "get_step_time"):
                    current_date = trade_calendar.get_step_time(
                        min(int(trade_step), trade_calendar.get_trade_len() - 1)
                    )[0]
                else:
                    current_date = trade_calendar[min(int(trade_step), len(trade_calendar) - 1)]
                if hasattr(current_date, "strftime"):
                    current_date = current_date.strftime("%Y-%m-%d")

                progress_data = {
                    "backtest_id": self.backtest_id,
                    "status": "running",
                    "progress": progress,
                    "message": f"正在处理: {current_date}",
                    "type": "progress",
                }

                # 发送到进度频道
                self.redis_client.publish(
                    f"qlib:backtest:progress:{self.backtest_id}",
                    json.dumps(progress_data),
                )
                # 同时存入一个状态 Key 供查询
                self.redis_client.set(
                    f"qlib:backtest:status:{self.backtest_id}",
                    json.dumps(progress_data),
                    ex=3600,
                )
        except Exception as e:
            StructuredTaskLogger(
                logger,
                "redis-logger-mixin",
                {"backtest_id": self.backtest_id, "strategy": self.__class__.__name__},
            ).debug("progress_log_failed", "记录进度失败", error=e)

    def log_executed_trades(self, execute_result):
        if not self.redis_client or not execute_result:
            return

        try:
            redis_key = f"qlib:backtest:trades:{self.backtest_id}"

            # ... (保持原有的交易记录逻辑)

            for item in execute_result:
                # item structure: (Order, trade_val, trade_cost, trade_price)
                if not isinstance(item, tuple) or len(item) < 4:
                    continue

                order = item[0]
                trade_val = item[1]
                trade_cost = item[2]
                trade_price = item[3]

                # Check if order executed
                if not hasattr(order, "deal_amount") or order.deal_amount <= 0:
                    continue

                direction = "buy" if order.direction == OrderDir.BUY else "sell"

                date_str = "Unknown"
                if hasattr(order, "start_time"):
                    try:
                        date_str = order.start_time.strftime("%Y-%m-%d")
                    except:
                        pass

                cash_after = None
                position_value_after = None
                equity_after = None
                try:
                    pos_obj = None
                    if hasattr(self, "trade_position"):
                        tp = self.trade_position
                        # Qlib 的 trade_position 可能是 Account 对象(有 get_current_position)
                        # 或者直接是 Position 对象
                        if hasattr(tp, "get_current_position"):
                            pos_obj = tp.get_current_position()
                        else:
                            pos_obj = tp
                    if pos_obj is not None:
                        if hasattr(pos_obj, "get_cash"):
                            try:
                                cash_after = float(pos_obj.get_cash(include_settle=True))
                            except TypeError:
                                cash_after = float(pos_obj.get_cash())
                        if hasattr(pos_obj, "calculate_stock_value"):
                            position_value_after = float(pos_obj.calculate_stock_value())
                        if hasattr(pos_obj, "calculate_value"):
                            equity_after = float(pos_obj.calculate_value())
                        elif cash_after is not None and position_value_after is not None:
                            equity_after = cash_after + position_value_after
                except Exception:
                    pass

                adj_price = float(trade_price)
                adj_quantity = float(order.deal_amount)
                factor = getattr(order, "factor", None)
                factor_val = None
                if factor is not None:
                    try:
                        factor_val = float(factor)
                    except Exception:
                        factor_val = None

                # Qlib 内部成交通常使用复权口径（price/amount 受 factor 影响）。
                # 对外展示时转成更贴近日线行情的非复权口径，避免与行情终端收盘价对不上。
                display_price = adj_price
                display_quantity = adj_quantity
                if factor_val is not None and factor_val > 0:
                    display_price = adj_price / factor_val
                    display_quantity = adj_quantity * factor_val

                record = {
                    "date": date_str,
                    "symbol": str(order.stock_id),
                    "action": direction,
                    "quantity": _normalize_display_quantity(str(order.stock_id), display_quantity),
                    "price": float(display_price),
                    "commission": float(trade_cost),
                    "totalAmount": float(trade_val),
                    "cash_after": cash_after,
                    "position_value_after": position_value_after,
                    "equity_after": equity_after,
                    # 追踪字段：保留复权口径，便于和 Qlib 内部计算对账
                    "adj_price": adj_price,
                    "adj_quantity": adj_quantity,
                    "factor": factor_val,
                    # 兼容旧前端字段命名
                    "balance": equity_after,
                    "type": "trade",
                }

                self.redis_client.rpush(redis_key, json.dumps(record))

            self.redis_client.expire(redis_key, 3600)

        except Exception as e:
            StructuredTaskLogger(
                logger,
                "redis-logger-mixin",
                {"backtest_id": self.backtest_id, "strategy": self.__class__.__name__},
            ).error("trade_log_failed", "记录交易日志失败", error=e)


class DynamicRiskMixin:
    """动态风险仓位支持"""

    def init_dynamic_risk(self, kwargs):
        self.market_state_series = kwargs.pop("market_state_series", None)
        self.position_by_state = kwargs.pop("position_by_state", None)
        self.strategy_total_position = kwargs.pop("strategy_total_position", None)
        self.default_risk_degree = kwargs.pop("risk_degree", None)
        self.account_stop_loss = float(kwargs.pop("account_stop_loss", 0.0))
        self.max_leverage = float(kwargs.pop("max_leverage", 1.0))
        self._is_account_stopped = False
        self._initial_account_value = None

    def get_risk_degree(self, *args, **kwargs):
        trade_date = self._get_trade_date()
        if trade_date is not None:
            date_key = trade_date.strftime("%Y-%m-%d")
            if isinstance(self.market_state_series, dict):
                value = self.market_state_series.get(date_key)
                if isinstance(value, (int, float)):
                    return self._clamp(float(value))
                if isinstance(value, str) and isinstance(self.position_by_state, dict):
                    mapped = self.position_by_state.get(value, self.position_by_state.get("neutral", 1.0))
                    base = float(self.strategy_total_position) if self.strategy_total_position is not None else 1.0
                    # Enforce max leverage limit
                    return self._clamp(min(mapped * base, self.max_leverage))

        if self.default_risk_degree is not None:
            try:
                # Enforce max leverage limit on default risk degree too
                return self._clamp(min(float(self.default_risk_degree), self.max_leverage))
            except Exception:
                pass
        return self._clamp(min(super().get_risk_degree(*args, **kwargs), self.max_leverage))

    def reset_dynamic_risk(self):
        """回测重置时清除止损状态和初始本金记录，避免跨轮次污染。"""
        self._initial_account_value = None
        self._is_account_stopped = False

    def check_account_stop_loss(self):
        """Check if the account value has dropped below the stop-loss threshold."""
        if self._is_account_stopped:
            return True

        # 0.0 表示禁用
        if self.account_stop_loss == 0.0:
            return False

        # 兼容两种格式：
        #   正数比例 (如 0.8)  → 净值跌破初始值的 80% 时触发
        #   负数回撤 (如 -0.2) → 净值从初始值下跌 20% 时触发（等同于 0.8 格式）
        if self.account_stop_loss > 0:
            threshold_ratio = self.account_stop_loss
        else:
            threshold_ratio = 1.0 + self.account_stop_loss  # -0.2 → 0.8

        if threshold_ratio <= 0.0 or threshold_ratio >= 1.0:
            return False

        try:
            pos_obj = None
            if hasattr(self, "trade_position"):
                tp = self.trade_position
                if hasattr(tp, "get_current_position"):
                    pos_obj = tp.get_current_position()
                else:
                    pos_obj = tp

            if pos_obj is None:
                return False

            current_value = float(pos_obj.calculate_value())

            if self._initial_account_value is None:
                self._initial_account_value = current_value
                return False

            if current_value < self._initial_account_value * threshold_ratio:
                StructuredTaskLogger(
                    logger,
                    "dynamic-risk",
                    {
                        "strategy": self.__class__.__name__,
                        "backtest_id": getattr(self, "backtest_id", None),
                    },
                ).warning(
                    "account_stop_loss_triggered",
                    "Account stop-loss triggered",
                    current_value=f"{current_value:,.0f}",
                    threshold=f"{self._initial_account_value * threshold_ratio:,.0f}",
                    initial=f"{self._initial_account_value:,.0f}",
                    stop_loss=self.account_stop_loss,
                )
                self._is_account_stopped = True
                return True
        except Exception as e:
            StructuredTaskLogger(
                logger,
                "dynamic-risk",
                {
                    "strategy": self.__class__.__name__,
                    "backtest_id": getattr(self, "backtest_id", None),
                },
            ).debug("account_stop_loss_check_failed", "Failed to check account stop-loss", error=e)

        return False

    def _liquidate_all(self):
        import logging

        from qlib.backtest.decision import Order, OrderDir, TradeDecisionWO

        StructuredTaskLogger(
            logger,
            "dynamic-risk",
            {
                "strategy": self.__class__.__name__,
                "backtest_id": getattr(self, "backtest_id", None),
            },
        ).warning("liquidate_all", "Liquidating all positions due to account stop loss")
        current_position = getattr(self, "trade_position", None)
        orders = []
        if current_position is not None:
            current_stocks = current_position.get_stock_list()
            trade_date = self._get_trade_date()
            for stock in current_stocks:
                amount = current_position.get_stock_amount(stock)
                if abs(amount) > 1e-4:
                    direction = OrderDir.SELL if amount > 0 else OrderDir.BUY
                    orders.append(
                        Order(
                            stock_id=stock,
                            amount=abs(amount),
                            direction=direction,
                            start_time=trade_date,
                            end_time=trade_date,
                        )
                    )
        return TradeDecisionWO(orders, self)

    def _get_trade_date(self):
        trade_step = getattr(self, "trade_step", None)
        trade_calendar = getattr(self, "trade_calendar", None)
        if trade_calendar is None:
            exchange = getattr(self, "trade_exchange", None)
            if exchange is not None and hasattr(exchange, "trade_calendar"):
                trade_calendar = exchange.trade_calendar
        if trade_calendar is None or trade_step is None:
            return None
        try:
            if hasattr(trade_calendar, "get_step_time"):
                return trade_calendar.get_step_time(min(int(trade_step), trade_calendar.get_trade_len() - 1))[0]
            return trade_calendar[min(int(trade_step), len(trade_calendar) - 1)]
        except Exception:
            return None

    def _clamp(self, value: float) -> float:
        return max(0.0, min(1.0, value))

    def _get_trade_step_safe(self):
        """兼容不同 qlib 版本的交易步长读取。"""
        trade_calendar = getattr(self, "trade_calendar", None)
        if trade_calendar is not None and hasattr(trade_calendar, "get_trade_step"):
            try:
                return int(trade_calendar.get_trade_step())
            except Exception:
                pass
        trade_step = getattr(self, "trade_step", None)
        if trade_step is not None:
            try:
                return int(trade_step)
            except Exception:
                pass
        return None

    def _should_rebalance(self, rebalance_days: int) -> bool:
        """统一调仓周期判定；无法读取交易步长时回退到本地计数器。"""
        if int(rebalance_days) <= 1:
            return True
        step = self._get_trade_step_safe()
        if step is None:
            step = int(getattr(self, "_qm_trade_step_counter", 0))
            self._qm_trade_step_counter = step + 1
        return step % int(rebalance_days) == 0

    def reset(self, *args, **kwargs):
        """
        兼容 qlib 0.9.7+ 在 backtest_loop 中传入 reset(level_infra=...) 的调用方式。
        旧签名不接受该参数时自动降级重试。
        """
        self._qm_trade_step_counter = 0
        self.reset_dynamic_risk()
        try:
            return super().reset(*args, **kwargs)
        except TypeError as exc:
            msg = str(exc)
            if "unexpected keyword argument" not in msg:
                raise
            filtered = dict(kwargs)
            filtered.pop("level_infra", None)
            filtered.pop("common_infra", None)
            filtered.pop("trade_exchange", None)
            try:
                return super().reset(*args, **filtered)
            except TypeError:
                return super().reset()


class RedisRecordingStrategy(DynamicRiskMixin, TopkDropoutStrategy, RedisLoggerMixin):
    """
    带有 Redis 记录功能的 TopkDropout 策略
    """

    def __init__(self, *args, **kwargs):
        # 提取调仓周期参数
        self.rebalance_days = int(kwargs.pop("rebalance_days", 1))
        StructuredTaskLogger(
            logger,
            "redis-recording-strategy",
            {"rebalance_days": self.rebalance_days},
        ).info("init", "RedisRecordingStrategy initialized")

        # 1. 初始化我们自定义的 mixin
        self.init_redis(kwargs)
        self.init_dynamic_risk(kwargs)

        # 2. 统一清除所有「本项目自定义 / 前端传入」的 kwargs，
        #    这些字段 Qlib BaseStrategy 不接受。
        clean_kwargs = {k: v for k, v in kwargs.items() if k not in _OUR_KWARGS}

        # 3. 调用 super().__init__
        super().__init__(*args, **clean_kwargs)

    def generate_trade_decision(self, execute_result=None):
        # 0. 账户止损检查
        if self.check_account_stop_loss():
            StructuredTaskLogger(
                logger,
                "redis-recording-strategy",
                {"rebalance_days": self.rebalance_days, "backtest_id": getattr(self, "backtest_id", None)},
            ).info("account_stop_loss", "Account stop-loss triggered. Liquidating.")
            return self._liquidate_all()

        # 调仓周期控制
        # trade_step 是 BaseStrategy 维护的当前步数索引
        trade_step = self._get_trade_step_safe() or 0

        # 如果设置了调仓周期且当前不是调仓日，跳过调仓
        if self.rebalance_days > 1 and trade_step % self.rebalance_days != 0:
            from qlib.backtest.decision import TradeDecisionWO

            return TradeDecisionWO([], self)

        # Generate new orders (交易记录已移至 post_exe_step，此处不再重复记录)
        trade_decision = super().generate_trade_decision(execute_result)
        return trade_decision

    def reset(self, *args, **kwargs):
        """兼容 qlib reset 签名差异（level_infra/common_infra/trade_exchange）。"""
        self._qm_trade_step_counter = 0
        try:
            return super().reset(*args, **kwargs)
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            filtered = dict(kwargs)
            filtered.pop("level_infra", None)
            filtered.pop("common_infra", None)
            filtered.pop("trade_exchange", None)
            try:
                return super().reset(*args, **filtered)
            except TypeError:
                return super().reset()

    def post_exe_step(self, execute_result=None):
        """每个执行步骤完成后由框架回调，记录本步所有成交并更新进度。
        相比在 generate_trade_decision 中记录上一步结果，此处可捕获最后一天的交易。
        """
        self.log_progress()
        self.log_executed_trades(execute_result)


class SimpleWeightStrategy(WeightStrategyBase):
    """
    简单权重策略：根据预测分数为正的股票分配权重（归一化）
    """

    def __init__(self, *args, topk=None, min_score=0.0, max_weight=1.0, **kwargs):
        self.topk = int(topk) if topk is not None else None
        self.min_score = float(min_score) if min_score is not None else 0.0
        self.max_weight = float(max_weight) if max_weight is not None else 1.0
        super().__init__(*args, **kwargs)

    def _build_capped_weights(self, scores: pd.Series, max_weight: float) -> pd.Series:
        weights = pd.Series(0.0, index=scores.index)
        remaining = scores.copy()
        remaining_weight = 1.0

        while not remaining.empty and remaining_weight > 0:
            scaled = remaining / remaining.sum() * remaining_weight
            over = scaled > max_weight
            if not over.any():
                weights.loc[remaining.index] = scaled
                break

            weights.loc[scaled[over].index] = max_weight
            remaining_weight = 1.0 - weights.sum()
            if remaining_weight <= 0:
                total = weights.sum()
                if total > 0:
                    weights = weights / total
                break
            remaining = remaining.loc[~over]
        return weights[weights > 0]

    def generate_target_weight_position(self, score, current=None, trade_exchange=None, *args, **kwargs):
        if current is None and args:
            current = args[0]
        if trade_exchange is None and len(args) > 1:
            trade_exchange = args[1]
        if score is None or score.empty:
            return {}

        # 过滤 NaN
        sc = score.dropna()

        # 确保 sc 是 Series 格式
        if isinstance(sc, pd.DataFrame):
            if sc.shape[1] > 0:
                sc = sc.iloc[:, 0]
            else:
                return {}

        # 只保留大于阈值的正分
        threshold = self.min_score if self.min_score is not None else 0.0
        sc = sc[sc > threshold]

        if self.topk and self.topk > 0 and len(sc) > self.topk:
            sc = sc.nlargest(self.topk)

        if sc.empty:
            return {}

        # 归一化
        total = sc.sum()
        if total <= 0:
            return {}

        if self.max_weight is not None and 0 < self.max_weight < 1.0:
            weights = self._build_capped_weights(sc, self.max_weight)
        else:
            weights = sc / total
        return weights.to_dict()


class RedisWeightStrategy(DynamicRiskMixin, SimpleWeightStrategy, RedisLoggerMixin):
    """
    带有 Redis 记录功能的 SimpleWeightStrategy，并在选股层过滤涨停/停牌股。

    与 TopkDropoutStrategy 不同，WeightStrategyBase 不支持 only_tradable。
    这里通过覆写 generate_target_weight_position，在归一化权重前剔除：
    - 涨停股（limit_buy=True）：买入无法成交，不应占用权重
    - 停牌股（suspended=True）：同上
    跌停股的卖出由交易所执行层自动拦截，不在此处处理。
    """

    def __init__(self, *args, **kwargs):
        self.init_redis(kwargs)
        self.init_dynamic_risk(kwargs)
        self.rebalance_days = int(kwargs.pop("rebalance_days", 1))
        StructuredTaskLogger(
            logger,
            "redis-weight-strategy",
            {"rebalance_days": self.rebalance_days, "backtest_id": getattr(self, "backtest_id", None)},
        ).info("init", "RedisWeightStrategy initialized")
        clean_kwargs = {k: v for k, v in kwargs.items() if k not in _OUR_KWARGS}
        super().__init__(*args, **clean_kwargs)

    def reset(self, *args, **kwargs):
        """兼容 qlib reset 签名差异（level_infra/common_infra/trade_exchange）。"""
        self._qm_trade_step_counter = 0
        try:
            return super().reset(*args, **kwargs)
        except TypeError as exc:
            if "unexpected keyword argument" not in str(exc):
                raise
            filtered = dict(kwargs)
            filtered.pop("level_infra", None)
            filtered.pop("common_infra", None)
            filtered.pop("trade_exchange", None)
            try:
                return super().reset(*args, **filtered)
            except TypeError:
                return super().reset()

    def generate_target_weight_position(self, score, current=None, trade_exchange=None, *args, **kwargs):
        if self.check_account_stop_loss():
            StructuredTaskLogger(
                logger,
                "redis-weight-strategy",
                {"rebalance_days": self.rebalance_days, "backtest_id": getattr(self, "backtest_id", None)},
            ).info("account_stop_loss", "Account stop-loss triggered. Target position is empty.")
            return {}

        exchange = trade_exchange or getattr(self, "trade_exchange", None)
        t_start = kwargs.get("trade_start_time") or kwargs.get("t_start")
        t_end = kwargs.get("trade_end_time") or kwargs.get("t_end") or t_start

        # 在归一化权重前过滤涨停/停牌股（不可买入，不应占权重名额）
        if exchange is None:
            StructuredTaskLogger(
                logger,
                "redis-weight-strategy",
                {"rebalance_days": self.rebalance_days, "backtest_id": getattr(self, "backtest_id", None)},
            ).warning("trade_exchange_missing", "trade_exchange 未注入，跳过涨停过滤")
        elif score is not None and not score.empty and t_start is not None:
            if isinstance(score, pd.DataFrame):
                score = score.iloc[:, 0]
            filtered_index = []
            skipped = 0
            for sid in score.index:
                try:
                    if exchange.check_stock_suspended(sid, t_start, t_end) or exchange.check_stock_limit(
                        sid, t_start, t_end, direction=Order.BUY
                    ):
                        skipped += 1
                        continue
                except Exception:
                    pass
                filtered_index.append(sid)
            if skipped:
                StructuredTaskLogger(
                    logger,
                    "redis-weight-strategy",
                    {"rebalance_days": self.rebalance_days, "backtest_id": getattr(self, "backtest_id", None)},
                ).info(
                    "trade_filter",
                    "剔除涨停/停牌标的",
                    trade_date=t_start.date(),
                    skipped=skipped,
                )
                score = score.loc[filtered_index]
            else:
                StructuredTaskLogger(
                    logger,
                    "redis-weight-strategy",
                    {"rebalance_days": self.rebalance_days, "backtest_id": getattr(self, "backtest_id", None)},
                ).info(
                    "trade_filter",
                    "检查标的，无涨停/停牌",
                    trade_date=t_start.date(),
                    checked=len(score),
                )

        return super().generate_target_weight_position(score, current, trade_exchange, *args, **kwargs)

    def _safe_generate_trade_decision(self, execute_result=None):
        """安全地生成交易决策，处理 get_deal_price 返回 None 的情况。"""
        try:
            return super().generate_trade_decision(execute_result)
        except TypeError as e:
            if "unsupported operand type(s) for /: 'float' and 'NoneType'" in str(e):
                from qlib.backtest.decision import TradeDecisionWO
                StructuredTaskLogger(
                    logger,
                    "redis-weight-strategy",
                    {"backtest_id": getattr(self, "backtest_id", None)},
                ).warning("skip_trade_no_price", "Skip trade due to missing price data")
                return TradeDecisionWO([], self)
            raise

    def generate_trade_decision(self, execute_result=None):
        if not self._should_rebalance(self.rebalance_days):
            from qlib.backtest.decision import TradeDecisionWO

            return TradeDecisionWO([], self)

        current_step = self._get_trade_step_safe() or 0
        StructuredTaskLogger(
            logger,
            "redis-weight-strategy",
            {"rebalance_days": self.rebalance_days, "backtest_id": getattr(self, "backtest_id", None)},
        ).info("generate_orders", "Generating orders", step=current_step)
        return self._safe_generate_trade_decision(execute_result)

    def post_exe_step(self, execute_result=None):
        self.log_progress()
        self.log_executed_trades(execute_result)
