import json
import logging
from contextlib import contextmanager

import pandas as pd
from qlib.backtest.decision import Order, OrderDir
from qlib.contrib.strategy.signal_strategy import (
    TopkDropoutStrategy,
    WeightStrategyBase,
)

import redis

from backend.shared.fundamental_aligner import fundamental_aligner
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
    "pool_file_local",
    "pool_file_key",
    "pool_file_url",
    # AI/前端策略生成参数（由平台上层消费，不传给 qlib BaseStrategy）
    "condition",
    "conditions",
    "selection_condition",
    "position_config",
    "style_params",
    # 融资融券相关字段
    "financing_rate",
    "borrow_rate",
    "max_short_exposure",
    "max_leverage",
    "account_stop_loss",
    # 调仓周期（各策略自行 pop 使用，不传给 BaseStrategy）
    "rebalance_days",
    # 策略自定义参数（由上层策略类自行 pop 消费，不传给 Qlib BaseStrategy）
    "momentum_period",
    "vol_lookback",
    "min_score",
    "short_topk",
    "long_exposure",
    "short_exposure",
    "enable_short_selling",
    "stop_loss",
    "take_profit",
}


def strip_unsupported_kwargs(cls, kwargs: dict, *, strategy_name: str = "") -> dict:
    """剔除 qlib 策略基类签名不接受的 kwargs，原地修改并返回同一 dict。

    前端/AI 生成的策略配置常携带平台侧参数（如 momentum_period），而 qlib 的
    BaseStrategy 使用严格签名，收到未知关键字会直接抛 TypeError 使回测失败。

    注意：qlib 各层策略基类几乎都声明了 **kwargs 并逐层下传，最终收敛到
    BaseStrategy 这一唯一严格签名，因此不能因为「某基类有 **kwargs」就放行。
    这里按 MRO 收集所有具名参数的并集作为合法集合。
    """
    import inspect

    accepted: set[str] = set()
    for base in inspect.getmro(cls):
        init = base.__dict__.get("__init__")
        if init is None:
            continue
        try:
            sig = inspect.signature(init)
        except (TypeError, ValueError):
            continue
        for name, param in sig.parameters.items():
            if name == "self":
                continue
            if param.kind in (
                inspect.Parameter.VAR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
            ):
                continue
            accepted.add(name)

    unknown = [k for k in kwargs if k not in accepted]
    for key in unknown:
        kwargs.pop(key, None)
    if unknown:
        logger.warning(
            "%s: 忽略 qlib 不支持的策略参数 %s（该参数未被策略实现消费）",
            strategy_name or cls.__name__,
            sorted(unknown),
        )
    return kwargs


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

    def _market_state_risk_degree(self, trade_date):
        """按大盘状态序列算仓位；未配置动态仓位或该日无状态时返回 None。

        抽出来是为了让 TopkDropout 系策略也能用：那条链路不会调用
        ``get_risk_degree``，只能通过 ``_dynamic_risk_degree`` 钩子接入。
        """
        series = getattr(self, "market_state_series", None)
        if trade_date is None or not isinstance(series, dict):
            return None
        value = series.get(trade_date.strftime("%Y-%m-%d"))
        if isinstance(value, (int, float)):
            return self._clamp(float(value))
        position_by_state = getattr(self, "position_by_state", None)
        if isinstance(value, str) and isinstance(position_by_state, dict):
            mapped = position_by_state.get(value, position_by_state.get("neutral", 1.0))
            total = getattr(self, "strategy_total_position", None)
            base = float(total) if total is not None else 1.0
            return self._clamp(min(mapped * base, getattr(self, "max_leverage", 1.0)))
        return None

    def get_risk_degree(self, *args, **kwargs):
        market_degree = self._market_state_risk_degree(self._get_trade_date())
        if market_degree is not None:
            return market_degree

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


class FundamentalFilterMixin:
    """
    基本面硬过滤混入类
    支持 kwargs 中 `f_` 前缀参数，映射到 FundamentalAligner 约束。
    """

    def init_fundamental_filter(self, kwargs):
        self.fundamental_constraints = {}
        for key in list(kwargs.keys()):
            if key.startswith("f_"):
                self.fundamental_constraints[key[2:]] = kwargs.pop(key)

        # 兼容旧参数
        for old_key in ["pe_max", "mc_min", "mc_max", "exclude_st"]:
            if old_key in kwargs:
                if old_key == "exclude_st" and kwargs[old_key]:
                    self.fundamental_constraints["is_st_not"] = 1
                    kwargs.pop(old_key)
                else:
                    self.fundamental_constraints[old_key] = kwargs.pop(old_key)

        self.use_fundamental_filter = len(self.fundamental_constraints) > 0

    def apply_fundamental_filter(self, score, trade_date):
        if not self.use_fundamental_filter or score is None or score.empty:
            return score

        index = score.index
        # SimpleSignal 返回的是「只按 instrument 索引」的 Series，
        # qlib 原生 DataFrameSignal 返回 (datetime, instrument) 两级索引，两种都要支持。
        if isinstance(index, pd.MultiIndex) and "instrument" in (index.names or []):
            instrument_values = index.get_level_values("instrument")
        else:
            instrument_values = pd.Index(index)

        instruments = [str(v) for v in instrument_values.tolist()]
        filtered_list = fundamental_aligner.filter_instruments(
            trade_date, instruments, constraints=self.fundamental_constraints
        )
        if not filtered_list:
            # 过滤后为空：返回同结构的空对象，交给上层按「当天不调仓」处理。
            return score.iloc[:0]

        # filter_instruments 内部按前缀式比较，但返回的是传入格式的子集
        # （qlib 信号里是小写 sh600000），所以直接用原值做集合判断，不要二次规范化。
        filtered_set = set(filtered_list)
        keep = [instrument in filtered_set for instrument in instruments]
        return score[keep]


class _FundamentalFilteredSignal:
    """把 ``signal.get_signal()`` 的结果按 ``f_*`` 约束过滤后再交给策略选股。

    为什么需要它：qlib 的 ``TopkDropoutStrategy.generate_trade_decision`` 自己直接读
    ``self.signal.get_signal(...)`` 做 Top-K 选股，**从不调用**
    ``generate_target_weight_position``。因此只在
    ``generate_target_weight_position`` 里做基本面过滤（历史写法）对 Topk-Dropout 系
    策略完全无效——``f_*`` 会被静默忽略。这里把 signal 临时包一层，让过滤真正生效。
    """

    def __init__(self, inner, apply_filter, trade_date):
        self._inner = inner
        self._apply_filter = apply_filter
        self._trade_date = trade_date

    def get_signal(self, *args, **kwargs):
        score = self._inner.get_signal(*args, **kwargs)
        return self._apply_filter(score, self._trade_date)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _ScoreAdjustedSignal:
    """把 ``signal.get_signal()`` 的结果交给策略自定义钩子调整后再交给选股逻辑。

    为什么需要它：qlib 的 ``TopkDropoutStrategy.generate_trade_decision`` 自己读
    ``self.signal.get_signal(...)`` 做 Top-K 选股，**从不调用**
    ``generate_target_weight_position``。自定义选股逻辑（动量融合、趋势闸门、
    涨停规避…）如果只覆写后者，会被静默忽略——模板看着像在跑，其实和内置类
    逐字节同结果。这里把 signal 临时包一层，让钩子真正生效。

    ``ref_date`` 固定传上一交易日（T-1）：回测是 T-1 收盘出信号、T 日开盘成交，
    钩子只能看到 T 日开盘前已知的数据，避免前视偏差。
    """

    def __init__(self, inner, adjust, ref_date):
        self._inner = inner
        self._adjust = adjust
        self._ref_date = ref_date

    def get_signal(self, *args, **kwargs):
        score = self._inner.get_signal(*args, **kwargs)
        if score is None:
            return score
        try:
            adjusted = self._adjust(score, self._ref_date)
        except Exception:  # noqa: BLE001 - 钩子异常不能让整轮回测挂掉
            logger.exception("自定义选股钩子执行失败，本次沿用原始信号")
            return score
        return score if adjusted is None else adjusted

    def __getattr__(self, name):
        return getattr(self._inner, name)


class PriceFrameMixin:
    """日频行情矩阵缓存：首次调用把回测区间一次性取满，之后只做 pandas 切片。

    为什么需要：容器内 ``D.features`` 对每个标的都有约 2ms 的固定开销，
    全市场（约 5500 只）取一次要 10~17 秒，而且**不跨调用复用**。自定义类如果
    每个调仓步各调一次（动量融合、趋势闸门、涨停统计、逆波动加权），
    一年的回测会被从 1 分钟拖到 1 小时——实测 as11 单次回测跑了 60 分钟没结束。
    这里把「回测首日往前 buffer + 回测末日」一次性取全并缓存，后续每个调仓步
    只做毫秒级切片；回测中途新出现的标的按增量补取。

    缓存挂在实例上（``_qm_price_cache``），按字段列表分桶，跨调仓步共享。
    """

    # 回测首日之前还要多取一段，供 60 日动量、250 日回撤这类回看窗口使用。
    _PRICE_CACHE_BUFFER_DAYS = 400

    def _backtest_window(self):
        """回测区间 (首日, 末日)；日历不可用时返回 None（退化为按需取数）。"""
        calendar = getattr(self, "trade_calendar", None)
        if calendar is None:
            return None
        try:
            first = pd.Timestamp(calendar.get_step_time(0)[0])
            last = pd.Timestamp(calendar.get_step_time(calendar.get_trade_len() - 1)[0])
        except Exception:  # noqa: BLE001 - 日历形态随 qlib 版本变化，取不到就按需取
            return None
        return first, last

    def _price_frame(self, instruments, fields, start, end):
        """取 index=(日期, 标的)、columns=字段 的行情切片（只含本次请求的标的）。"""
        symbols = [str(symbol) for symbol in instruments]
        if not symbols:
            return None
        fields = list(fields)
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        cache = getattr(self, "_qm_price_cache", None)
        if cache is None:
            cache = {}
            self._qm_price_cache = cache
        key = tuple(fields)
        entry = cache.get(key)
        window = self._backtest_window()
        if window is None:
            fetch_start, fetch_end = start, end
        else:
            fetch_start = min(
                start, window[0] - pd.Timedelta(days=self._PRICE_CACHE_BUFFER_DAYS)
            )
            fetch_end = max(end, window[1])
        if entry is None or fetch_start < entry["start"] or fetch_end > entry["end"]:
            frame = self._fetch_price_frame(symbols, fields, fetch_start, fetch_end)
            if frame is None or frame.empty:
                return None
            frame = self._normalize_price_frame(frame)
            entry = {
                "frame": frame,
                "symbols": set(symbols),
                "start": fetch_start,
                "end": fetch_end,
            }
            cache[key] = entry
        missing = [symbol for symbol in symbols if symbol not in entry["symbols"]]
        if missing:
            extra = self._fetch_price_frame(missing, fields, entry["start"], entry["end"])
            if extra is not None and not extra.empty:
                extra = self._normalize_price_frame(extra)
                entry["frame"] = entry["frame"].combine_first(extra)
                entry["symbols"].update(missing)
        sliced = entry["frame"].loc[start:end]
        if sliced.empty:
            return None
        if set(symbols) != entry["symbols"]:
            # 缓存里可能还留着其它调仓步带进来的标的，只返回本次请求的那些
            # （否则 _close_matrix 的第 0 列可能不是调用方要的那只票）
            sliced = sliced[sliced.index.get_level_values("instrument").isin(symbols)]
            if sliced.empty:
                return None
        return sliced

    def _close_matrix(self, instruments, start, end):
        """收盘价矩阵（index=日期、columns=标的），走 ``_price_frame`` 的缓存。"""
        frame = self._price_frame(instruments, ["$close"], start, end)
        if frame is None:
            return None
        prices = frame["$close"].unstack(level="instrument")
        return prices.sort_index()

    @staticmethod
    def _normalize_price_frame(frame):
        """把 D.features 的多级索引统一成 (datetime, instrument) 顺序。

        本平台的 qlib 返回的是 instrument 在前的索引，直接 ``.loc[start:end]``
        会拿 Timestamp 和字符串比大小（TypeError: '<' not supported between
        instances of 'str' and 'Timestamp'）。统一后按日期切片的语义才成立。
        """
        if not isinstance(frame.index, pd.MultiIndex):
            return frame
        names = list(frame.index.names)
        if not names or names[0] == "datetime":
            return frame
        return frame.swaplevel("datetime", "instrument").sort_index()

    @staticmethod
    def _fetch_price_frame(symbols, fields, start, end):
        """真正的取数口子；失败只记日志并返回 None，由调用方按「不调整」处理。"""
        try:
            from backend.services.engine.qlib_app.utils.qlib_utils import D

            return D.features(list(symbols), list(fields), start, end, freq="day")
        except Exception:  # noqa: BLE001 - 单次取数失败不应让整轮回测挂掉
            logger.exception(
                "行情切片拉取失败（%d 只标的，%s → %s）", len(symbols), start, end
            )
            return None


class RedisRecordingStrategy(
    PriceFrameMixin,
    DynamicRiskMixin,
    FundamentalFilterMixin,
    TopkDropoutStrategy,
    RedisLoggerMixin,
):
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
        self.init_fundamental_filter(kwargs)

        # 2. 统一清除所有「本项目自定义 / 前端传入」的 kwargs，
        #    这些字段 Qlib BaseStrategy 不接受。
        clean_kwargs = {k: v for k, v in kwargs.items() if k not in _OUR_KWARGS}
        strip_unsupported_kwargs(type(self), clean_kwargs, strategy_name="RedisRecordingStrategy")

        # 3. 调用 super().__init__
        super().__init__(*args, **clean_kwargs)

        # 4. 让 risk_degree 真正生效。TopkDropoutStrategy.generate_trade_decision 直接读
        #    self.risk_degree 下单（`value = cash * self.risk_degree / len(buy)`），
        #    而 DynamicRiskMixin.get_risk_degree() 在这条链路上根本不会被调用；
        #    risk_degree 又被 init_dynamic_risk 提前 pop 成了 default_risk_degree，
        #    不显式回写就会被 BaseSignalStrategy 的默认值 0.95 覆盖
        #    （实测 as40「固定 60% 仓位」实际一直按 95% 下单）。
        # getattr 兜底：单测里 init_dynamic_risk 会被 monkeypatch 掉，此时不应 AttributeError
        default_risk_degree = getattr(self, "default_risk_degree", None)
        if default_risk_degree is not None:
            try:
                self.risk_degree = self._clamp(
                    min(float(default_risk_degree), getattr(self, "max_leverage", 1.0))
                )
            except (TypeError, ValueError):
                logger.warning("risk_degree 解析失败，沿用 qlib 默认仓位 0.95")
    def generate_target_weight_position(self, score, current=None, trade_exchange=None, *args, **kwargs):
        # 仅对 WeightStrategyBase 系策略有效；TopkDropoutStrategy 不会走到这里，
        # 它的 f_* 过滤由 _fundamental_filtered_signal() 在 generate_trade_decision 里完成。
        t_start = kwargs.get("trade_start_time") or kwargs.get("t_start")
        if t_start:
            score = self.apply_fundamental_filter(score, t_start)
        return super().generate_target_weight_position(score, current, trade_exchange, *args, **kwargs)

    @contextmanager
    def _fundamental_filtered_signal(self, trade_step):
        """临时把 self.signal 换成带 f_* 过滤的代理（无约束时不生效）。

        过滤必须用「上一交易日」的 features_daily 快照，否则构成前视偏差：
        - 回测在 T 日开盘价成交（`deal_price=open`），模型信号也滞后一日
          （`signal_lag_days=1`），所以 T 日开盘时只能看到 T-1 日收盘为止的数据；
        - 而 features_daily 的 `dt=T` 分区是 T 日收盘后才生成的（`pct_change` 就是
          T 日自身的涨跌幅、`close` 就是 T 日收盘价），拿它过滤等于用当天收盘信息
          在当天开盘下单。实测该偏差能把 as32 这种含 `f_pct_change_min` 的模板
          推到年化 400%+、夏普 13，明显失真。
        qlib 的 `get_step_time(step, shift=1)` 返回上一个 bar（shift>0 = 更早），
        在 step=0 时取到回测区间开始前的交易日；取不到或解析异常时跳过过滤
        （宁可少一层约束，也不能引入未来信息）。
        """
        if not self.use_fundamental_filter:
            yield
            return
        try:
            current_time, _ = self.trade_calendar.get_step_time(trade_step)
            prev_time, _ = self.trade_calendar.get_step_time(trade_step, shift=1)
        except Exception:  # noqa: BLE001 - 日历不可用时退化为不过滤
            logger.warning("fundamental filter: 无法取到调仓日，跳过 f_* 过滤")
            yield
            return
        if pd.Timestamp(prev_time) >= pd.Timestamp(current_time):
            logger.warning(
                "fundamental filter: 上一交易日解析异常（%s >= %s），跳过 f_* 过滤",
                prev_time,
                current_time,
            )
            yield
            return
        original = self.signal
        self.signal = _FundamentalFilteredSignal(
            original, self.apply_fundamental_filter, prev_time
        )
        try:
            yield
        finally:
            self.signal = original

    # ---- 自定义策略钩子：默认不改变任何行为，供模板 .py 里的子类覆写 ----
    def _adjust_signal(self, score, ref_date):
        """子类钩子：TopK 选股前调整打分（动量融合 / 趋势闸门 / 涨停规避…）。

        ``ref_date`` 是上一交易日（T-1），只能读该日收盘为止的数据。
        返回 None 表示不调整；默认原样返回。
        """
        return score

    def _dynamic_risk_degree(self, base, ref_date):
        """子类钩子：按市场状态调整仓位系数（波动率目标 / 回撤阶梯…）。

        ``ref_date`` 同上（T-1）。返回 None 表示不调整。
        默认实现消费平台注入的 ``market_state_series``（UI 开启「动态仓位」时才有）：
        这条链路走的是 TopkDropout，不会调用 ``get_risk_degree``，只能在这里接。
        """
        market_degree = self._market_state_risk_degree(ref_date)
        return base if market_degree is None else market_degree

    def _prev_bar_time(self, trade_step):
        """取上一交易日的调仓时点；取不到返回 None（调用方按「不调整」处理）。"""
        try:
            prev_time, _ = self.trade_calendar.get_step_time(trade_step, shift=1)
            return pd.Timestamp(prev_time)
        except Exception:  # noqa: BLE001 - 日历不可用时退化为不调整
            return None

    @contextmanager
    def _custom_signal_hook(self, trade_step):
        """把 self.signal 临时包一层，让 ``_adjust_signal`` 在 qlib 读信号时生效。"""
        ref_date = self._prev_bar_time(trade_step)
        if ref_date is None:
            yield
            return
        original = self.signal
        self.signal = _ScoreAdjustedSignal(original, self._adjust_signal, ref_date)
        try:
            yield
        finally:
            self.signal = original

    def _adjusted_risk_degree(self, trade_step, base):
        """按 ``_dynamic_risk_degree`` 计算本次调仓要用的仓位系数。"""
        ref_date = self._prev_bar_time(trade_step)
        if ref_date is None:
            return base
        try:
            adjusted = self._dynamic_risk_degree(base, ref_date)
        except Exception:  # noqa: BLE001 - 钩子异常不能让整轮回测挂掉
            logger.exception("自定义仓位钩子执行失败，沿用基础仓位")
            return base
        if adjusted is None:
            return base
        return max(0.0, min(1.0, float(adjusted)))

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
        base_risk_degree = getattr(self, "risk_degree", 0.95)
        try:
            with self._fundamental_filtered_signal(trade_step):
                with self._custom_signal_hook(trade_step):
                    # TopkDropoutStrategy 直接读 self.risk_degree 下单，动态仓位必须在
                    # 调用前改写它（get_risk_degree 在 Topk-Dropout 链路里不会被调用）。
                    self.risk_degree = self._adjusted_risk_degree(
                        trade_step, base_risk_degree
                    )
                    trade_decision = super().generate_trade_decision(execute_result)
        except TypeError as e:
            if "unsupported operand type(s) for /: 'float' and 'NoneType'" in str(e):
                from qlib.backtest.decision import TradeDecisionWO
                StructuredTaskLogger(
                    logger,
                    "redis-recording-strategy",
                    {"backtest_id": getattr(self, "backtest_id", None)},
                ).warning("skip_trade_no_price", "Skip trade due to missing price data (suspended stock)")
                return TradeDecisionWO([], self)
            raise
        finally:
            self.risk_degree = base_risk_degree
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
        strip_unsupported_kwargs(type(self), kwargs, strategy_name="SimpleWeightStrategy")
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


class RedisWeightStrategy(
    PriceFrameMixin, DynamicRiskMixin, SimpleWeightStrategy, RedisLoggerMixin
):
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
        strip_unsupported_kwargs(type(self), clean_kwargs, strategy_name="RedisWeightStrategy")
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
