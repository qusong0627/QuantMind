import json
import logging
from datetime import datetime
from typing import Optional, Tuple

import numpy as np
import pandas as pd
from qlib.backtest.decision import Order, OrderDir
from qlib.backtest.exchange import Exchange
from qlib.backtest.position import BasePosition

from backend.shared.redis_sentinel_client import get_redis_sentinel_client

logger = logging.getLogger(__name__)
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

task_logger = StructuredTaskLogger(logger, "CnExchange")


class CnExchange(Exchange):
    """
    Custom Qlib Exchange for China A-Shares.
    Implements specific cost rules:
    - Commission: Rate + Min (e.g. 0.025%, min 5)
    - Stamp Duty: Sell only (e.g. 0.05%)
    - Transfer Fee: SH only (e.g. 0.001%, min 0.01)
    - Market Impact: Slippage based on volume participation (Square Root Law)
    """

    def __init__(
        self,
        commission: float = 0.00025,
        min_commission: float = 5.0,
        stamp_duty: float = 0.0005,
        transfer_fee: float = 0.00001,
        min_transfer_fee: float = 0.01,
        impact_cost_coefficient: float = 0.0005,  # 市场冲击成本系数（与 Schema 默认值保持一致）
        backtest_id: str | None = None,  # 新增 backtest_id 用于关联交易记录
        allow_short_selling: bool = False,
        **kwargs,
    ):
        # Pass dummy costs to super because we will calculate our own
        super().__init__(open_cost=0, close_cost=0, min_cost=0, **kwargs)
        self.commission = commission
        self.min_commission = min_commission
        self.stamp_duty = stamp_duty
        self.transfer_fee = transfer_fee
        self.min_transfer_fee = min_transfer_fee
        self.impact_cost_coefficient = impact_cost_coefficient
        self.backtest_id = backtest_id
        self.allow_short_selling = bool(allow_short_selling)

        # Redis client for logging trades
        self.redis_client = None
        self.quote_fallback_lookback_days = 10
        try:
            import os

            raw_lookback = os.getenv("QLIB_QUOTE_FALLBACK_LOOKBACK_DAYS", "10")
            self.quote_fallback_lookback_days = max(1, int(raw_lookback))
        except Exception:
            self.quote_fallback_lookback_days = 10
        if self.backtest_id:
            try:
                self.redis_client = get_redis_sentinel_client()
                self.trades_key = f"qlib:backtest:trades:{self.backtest_id}"
                # Set expire time for the key (e.g., 24 hours)
                self.redis_client.expire(self.trades_key, 86400)
            except Exception as e:
                task_logger.warning("redis_client_init_failed", "Failed to initialize Redis client in CnExchange", error=str(e))

    @staticmethod
    def _is_invalid_quote(value: object) -> bool:
        if value is None:
            return True
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return True
        return np.isnan(numeric) or np.isinf(numeric) or numeric <= 1e-8

    def _get_recent_valid_quote(
        self,
        stock_id: str,
        start_time: pd.Timestamp,
        end_time: pd.Timestamp,
        field: str,
        method: str = "ts_data_last",
    ) -> float | None:
        value = self.quote.get_data(stock_id, start_time, end_time, field=field, method=method)
        if not self._is_invalid_quote(value):
            return float(value)

        for offset in range(1, self.quote_fallback_lookback_days + 1):
            prev_start = pd.Timestamp(start_time) - pd.Timedelta(days=offset)
            prev_end = pd.Timestamp(end_time) - pd.Timedelta(days=offset)
            fallback_value = self.quote.get_data(stock_id, prev_start, prev_end, field=field, method=method)
            if self._is_invalid_quote(fallback_value):
                continue

            task_logger.warning(
                "quote_fallback_applied",
                "Quote fallback applied with previous valid data",
                stock_id=stock_id,
                field=field,
                requested_start=str(pd.Timestamp(start_time)),
                requested_end=str(pd.Timestamp(end_time)),
                fallback_start=str(prev_start),
                fallback_end=str(prev_end),
                fallback_value=float(fallback_value),
                lookback_days=offset,
            )
            return float(fallback_value)

        return None if self._is_invalid_quote(value) else float(value)

    def get_close(
        self,
        stock_id: str,
        start_time: pd.Timestamp,
        end_time: pd.Timestamp,
        method: str = "ts_data_last",
    ) -> float | None:
        return self._get_recent_valid_quote(stock_id, start_time, end_time, field="$close", method=method)

    def get_factor(
        self,
        stock_id: str,
        start_time: pd.Timestamp,
        end_time: pd.Timestamp,
    ) -> float | None:
        if stock_id not in self.quote.get_all_stock():
            return None
        return self._get_recent_valid_quote(stock_id, start_time, end_time, field="$factor", method="ts_data_last")

    def get_deal_price(
        self,
        stock_id: str,
        start_time: pd.Timestamp,
        end_time: pd.Timestamp,
        direction: OrderDir,
        method: str | None = "ts_data_last",
    ) -> float | None:
        if direction == OrderDir.SELL:
            pstr = self.sell_price
        elif direction == OrderDir.BUY:
            pstr = self.buy_price
        else:
            raise NotImplementedError("This type of input is not supported")

        deal_price = self._get_recent_valid_quote(stock_id, start_time, end_time, field=pstr, method=method or "ts_data_last")
        if method is not None and self._is_invalid_quote(deal_price):
            task_logger.warning(
                "deal_price_fallback_to_close",
                "Invalid deal price, falling back to close price",
                stock_id=stock_id,
                field=pstr,
                start_time=str(pd.Timestamp(start_time)),
                end_time=str(pd.Timestamp(end_time)),
            )
            deal_price = self.get_close(stock_id, start_time, end_time, method)
        return deal_price

    def deal_order(
        self,
        order: Order,
        trade_account: BasePosition | None = None,
        force_deal: bool = False,
        position: BasePosition | None = None,
        dealt_order_amount: dict | None = None,
    ) -> tuple[float, float, float]:
        """
        Override deal_order to log trades to Redis
        """
        # Call super method to execute the trade
        # Qlib Exchange.deal_order signature:
        # def deal_order(self, order, trade_account=None, position=None, dealt_order_amount=None)

        # NOTE: 'force_deal' is NOT in standard Qlib Exchange.deal_order
        # It seems it was added in our custom logic or passed by a custom executor?
        # If standard executor passes it, it might be in **kwargs but executor.py line 604
        # shows explicit arguments? No, executor usually calls with specific args.
        # The traceback showed:
        # trade_val, trade_cost, trade_price = self.trade_exchange.deal_order(
        # TypeError: CnExchange.deal_order() got an unexpected keyword argument 'dealt_order_amount'

        # So we MUST accept dealt_order_amount.
        # And when we call super(), we should pass what parent expects.

        # Parent expects: order, trade_account, position, dealt_order_amount
        # It does NOT expect force_deal.

        trade_val, trade_cost, trade_price = super().deal_order(
            order, trade_account=trade_account, position=position, dealt_order_amount=dealt_order_amount
        )

        # Log trade if it was executed and backtest_id is provided
        if self.redis_client and trade_val > 0:
            try:
                action = "buy" if order.direction == OrderDir.BUY else "sell"
                trade_record = {
                    "date": order.start_time.strftime("%Y-%m-%d"),
                    "symbol": str(order.stock_id),
                    "action": action,
                    "price": float(trade_price),
                    "quantity": float(order.deal_amount),
                    "amount": float(trade_val),
                    "commission": float(trade_cost),
                    "timestamp": datetime.now().isoformat(),
                }
                self.redis_client.rpush(self.trades_key, json.dumps(trade_record))
            except Exception:
                # Suppress errors to avoid interrupting backtest
                pass

        return trade_val, trade_cost, trade_price

    def calculate_cost(
        self,
        stock_id: str,
        trade_val: float,
        direction: int,
        market_volume_val: float = 0.0,
    ) -> float:
        """
        Calculate transaction cost
        :param market_volume_val: Total market volume value (price * volume) for the day
        """
        if trade_val <= 1e-5:
            return 0.0

        # 1. Commission (Buy & Sell)
        comm = max(trade_val * self.commission, self.min_commission)

        # 2. Transfer Fee (Buy & Sell, SH only)
        tf = 0.0
        if stock_id.upper().startswith("SH"):
            tf = max(trade_val * self.transfer_fee, self.min_transfer_fee)

        # 3. Stamp Duty (Sell only)
        tax = 0.0
        if direction == OrderDir.SELL:
            tax = trade_val * self.stamp_duty

        # 4. Market Impact Cost (Slippage)
        # Slippage approx = Coeff * sqrt(TradeValue / MarketVolumeValue)
        impact = 0.0
        if market_volume_val > 0:
            participation_rate = trade_val / market_volume_val
            # 采用平方根定律模拟冲击成本
            impact = trade_val * self.impact_cost_coefficient * np.sqrt(participation_rate)
            if participation_rate > 0.1:  # 参与率超过 10% 时额外警告
                task_logger.debug(
                    "high_participation_rate",
                    "High participation rate",
                    stock_id=stock_id,
                    participation_rate=f"{participation_rate:.2%}",
                )

        return comm + tf + tax + impact

    def _get_max_buy_amount(
        self,
        stock_id: str,
        trade_price: float,
        cash: float,
        market_volume_val: float = 0.0,
    ) -> float:
        """Calculate max buy amount given cash limit"""
        if cash <= 0:
            return 0.0

        is_sh = stock_id.upper().startswith("SH")
        rate_sum = self.commission
        if is_sh:
            rate_sum += self.transfer_fee

        # Initial estimate
        amount = cash / (trade_price * (1 + rate_sum))

        # Verify and adjust (Iterative approach to account for fixed costs and market impact)
        for _ in range(3):
            val = amount * trade_price
            cost = self.calculate_cost(stock_id, val, OrderDir.BUY, market_volume_val=market_volume_val)
            if val + cost <= cash:
                break
            else:
                ratio = cash / (val + cost)
                amount = amount * ratio

        return amount

    def quote_clipping(self, order: Order) -> Order | None:
        """
        Clip the order based on price limits.
        Use base class check_stock_limit which is standard in Qlib.
        """
        if self.check_stock_limit(order.stock_id, order.start_time, order.end_time, direction=order.direction):
            task_logger.info("skip_trade_by_limit", "Skip trade by price limit", stock_id=order.stock_id, start_time=str(order.start_time))
            order.deal_amount = 0.0
            return order

        return super().quote_clipping(order)

    def _calc_trade_info_by_order(
        self,
        order: Order,
        position: BasePosition | None,
        dealt_order_amount: dict,
    ) -> tuple[float, float, float]:
        """
        Calculation of trade info
        **NOTE**: Order will be changed in this function
        """
        trade_price = self.get_deal_price(order.stock_id, order.start_time, order.end_time, direction=order.direction)
        if trade_price is None or np.isnan(trade_price) or trade_price <= 0:
            return 0.0, 0.0, 0.0

        trade_price = float(trade_price)

        # 获取市场当日成交量 (用于计算冲击成本)
        market_volume = self.get_volume(order.stock_id, order.start_time, order.end_time)
        market_volume_val = float(market_volume) * trade_price if market_volume is not None else 0.0

        # Basic volume clipping
        order.factor = self.get_factor(order.stock_id, order.start_time, order.end_time)
        order.deal_amount = order.amount
        self._clip_amount_by_volume(order, dealt_order_amount)

        # Cash / Position limit clipping
        if order.direction == Order.SELL:
            if position is not None:
                if self.allow_short_selling:
                    order.deal_amount = self.round_amount_by_trade_unit(
                        order.deal_amount,
                        order.factor,
                    )
                else:
                    current_amount = (
                        position.get_stock_amount(order.stock_id) if position.check_stock(order.stock_id) else 0
                    )
                    if not np.isclose(order.deal_amount, current_amount):
                        order.deal_amount = self.round_amount_by_trade_unit(
                            min(current_amount, order.deal_amount),
                            order.factor,
                        )

                    trade_val = order.deal_amount * trade_price
                    trade_cost = self.calculate_cost(
                        order.stock_id,
                        trade_val,
                        Order.SELL,
                        market_volume_val=market_volume_val,
                    )
                    if position.get_cash() + trade_val < trade_cost:
                        order.deal_amount = 0

        elif order.direction == Order.BUY:
            if position is not None:
                if self.allow_short_selling:
                    # Margin limits are enforced by strategy level weights and account constraints.
                    # We bypass strict cash limitation here to allow borrowing cash (negative cash).
                    order.deal_amount = self.round_amount_by_trade_unit(order.deal_amount, order.factor)
                else:
                    cash = position.get_cash()
                    trade_val = order.deal_amount * trade_price
                    trade_cost = self.calculate_cost(
                        order.stock_id,
                        trade_val,
                        Order.BUY,
                        market_volume_val=market_volume_val,
                    )

                    if cash < trade_cost:
                        order.deal_amount = 0
                    elif cash < trade_val + trade_cost:
                        max_amount = self._get_max_buy_amount(
                            order.stock_id,
                            trade_price,
                            cash,
                            market_volume_val=market_volume_val,
                        )
                        order.deal_amount = self.round_amount_by_trade_unit(
                            min(max_amount, order.deal_amount), order.factor
                        )
                    else:
                        order.deal_amount = self.round_amount_by_trade_unit(order.deal_amount, order.factor)
            else:
                order.deal_amount = self.round_amount_by_trade_unit(order.deal_amount, order.factor)

        # Final Calculation
        trade_val = order.deal_amount * trade_price
        trade_cost = self.calculate_cost(
            order.stock_id,
            trade_val,
            order.direction,
            market_volume_val=market_volume_val,
        )

        return trade_price, trade_val, trade_cost
