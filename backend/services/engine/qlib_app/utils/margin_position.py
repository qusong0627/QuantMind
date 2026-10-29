"""Qlib margin account/position helpers for short-selling backtests."""

from __future__ import annotations

import logging
from typing import Any
from collections.abc import Iterator

import numpy as np

# 增加环境适配保护：trade-core 服务镜像中没有安装 qlib
try:
    from qlib.backtest.account import Account
    from qlib.backtest.position import Position

    try:
        from qlib.backtest.decision import Order
    except ImportError:
        from qlib.backtest.order import Order
    HAS_QLIB = True
except ImportError:
    # 占位符，防止 trade-core 启动崩溃
    class Account:
        pass

    class Position:
        pass

    class Order:
        pass

    HAS_QLIB = False

logger = logging.getLogger(__name__)
from backend.services.engine.qlib_app.utils.structured_logger import StructuredTaskLogger

task_logger = StructuredTaskLogger(logger, "MarginPosition")


class MarginPosition(Position):
    """
    针对 A 股融资融券优化的持仓实现：
    1. 限制杠杆：融券卖出所得现金被“冻结”，不增加可用现金 (usable cash)。
    2. 盈亏结算：平仓时才结算盈亏至可用现金。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 初始化冻结资金池（记录空头开仓时的原始价值）
        if "short_proceeds" not in self.position:
            self.position["short_proceeds"] = 0.0

    def _iter_position_items(self) -> Iterator[tuple[str, dict[str, float]]]:
        for stock_id, info in self.position.items():
            if not isinstance(info, dict):
                continue
            if "amount" not in info or "price" not in info:
                continue
            yield stock_id, info

    def get_stock_list(self):
        return [stock_id for stock_id, _ in self._iter_position_items()]

    def calculate_stock_value(self) -> float:
        """
        修正权益计算：
        总市值 = Sum(多头市值) + Sum(空头市值 [负值]) + 融券冻结资金
        注：融券冻结资金抵消了空头头寸的负市值，剩下的差额即为该空头头寸的浮动盈亏。
        """
        stock_value = 0
        for _, info in self._iter_position_items():
            stock_value += info["amount"] * info["price"]
        return stock_value + self.position.get("short_proceeds", 0.0)

    def _buy_stock(self, stock_id: str, trade_val: float, cost: float, trade_price: float) -> None:
        """买入逻辑：区分 正常买入多头 和 买入平仓空头"""
        old_amount = self.get_stock_amount(stock_id)

        if old_amount < 0:
            # --- 买入平空 (Cover Short) ---
            trade_amount = trade_val / trade_price
            cover_amount = min(abs(old_amount), trade_amount)
            entry_price = self.position[stock_id].get(
                "short_entry_price", self.get_stock_price(stock_id)
            )

            # 实现盈亏 = (借入卖出价 - 买入平仓价) * 数量 - 交易成本
            realized_pnl = (entry_price - trade_price) * cover_amount - cost

            # 只有实现的盈亏会改变可用现金
            self.position["cash"] += realized_pnl

            # 扣减对应的冻结资金基数
            short_entry_val = entry_price * cover_amount
            self.position["short_proceeds"] = max(0.0, self.position["short_proceeds"] - short_entry_val)

            # 更新持仓数量
            self.position[stock_id]["amount"] += trade_amount

            # 如果买入量超过了空头持仓，剩余部分转为普通多头
            if trade_amount > cover_amount:
                extra_val = (trade_amount - cover_amount) * trade_price
                self.position["cash"] -= extra_val
                self.position[stock_id]["price"] = trade_price  # 更新为多头入场价
                self.position[stock_id].pop("short_entry_price", None)

            if abs(self.position[stock_id]["amount"]) <= 1e-5:
                self._del_stock(stock_id)
        else:
            # 正常买入多头：直接扣减可用现金
            super()._buy_stock(stock_id, trade_val, cost, trade_price)

    def _sell_stock(self, stock_id: str, trade_val: float, cost: float, trade_price: float) -> None:
        """卖出逻辑：区分 卖出多头 和 融券开空"""
        old_amount = self.get_stock_amount(stock_id)

        if old_amount <= 0:
            # --- 融券开空 (Open/Increase Short) ---
            trade_amount = trade_val / trade_price

            # A 股逻辑：卖出所得现金被质押，不能用来买入其他股票。
            # 因此可用现金只扣除手续费，不增加。
            self.position["cash"] -= cost
            # 记录冻结资金
            self.position["short_proceeds"] += trade_val

            if old_amount == 0:
                self._init_stock(stock_id=stock_id, amount=-trade_amount, price=trade_price)
                self.position[stock_id]["short_entry_price"] = trade_price
            else:
                # 更新空头持仓的平均入场价（用原开仓成本摊薄，不能用盯市价）
                new_amount = old_amount - trade_amount
                old_entry = self.position[stock_id].get(
                    "short_entry_price", self.get_stock_price(stock_id)
                )
                new_avg_price = (abs(old_amount) * old_entry + trade_val) / abs(new_amount)
                self.position[stock_id]["amount"] = new_amount
                self.position[stock_id]["short_entry_price"] = new_avg_price
                self.position[stock_id]["price"] = trade_price
        else:
            # --- 卖出多头 ---
            # 如果卖出额超过多头持仓，则转化为一部分平仓，一部分新开空
            long_val = old_amount * trade_price
            if trade_val > long_val:
                # 先全平多头 (增加现金)
                super()._sell_stock(stock_id, long_val, cost, trade_price)
                # 再融券开空 (不增加现金)
                self._sell_stock(stock_id, trade_val - long_val, 0, trade_price)
            else:
                super()._sell_stock(stock_id, trade_val, cost, trade_price)


class MarginAccount(Account):
    """
    支持计息与真实多空结算的账户类：
    1. 每日对 融资额(负现金) 和 融券额(空头市值) 计息。
    2. 按请求的融资、融券年化利率及自然日计息。
    """

    def __init__(self, *args, financing_rate=0.08, borrow_rate=0.08, **kwargs):
        super().__init__(*args, **kwargs)
        self.financing_rate = float(financing_rate)
        self.borrow_rate = float(borrow_rate)
        self._last_interest_date = None
        self._pre_trade_snapshot: tuple[float, float] | None = None

    def update_order(self, order: Order, trade_val: float, cost: float, trade_price: float) -> None:
        if self.current_position.skip_update():
            return

        pre_amount = 0.0
        pre_price = trade_price
        if self.current_position.check_stock(order.stock_id):
            pre_amount = self.current_position.get_stock_amount(order.stock_id)
            pre_price = self.current_position.get_stock_price(order.stock_id)
            if pre_amount < 0:
                pre_price = self.current_position.position[order.stock_id].get(
                    "short_entry_price", pre_price
                )

        self._pre_trade_snapshot = (float(pre_amount), float(pre_price))
        try:
            return super().update_order(order, trade_val, cost, trade_price)
        finally:
            self._pre_trade_snapshot = None

    def update_state(self, t_start, t_end, trade_exchange, level_infra, **kwargs):
        # 1. 每日计息 (在 Qlib 更新净值前扣除)
        self._apply_daily_interest(t_start, t_end, trade_exchange)

        # 2. 调用父类更新账户状态（包含计算当前净值等）
        return super().update_state(t_start, t_end, trade_exchange, level_infra, **kwargs)

    def _apply_daily_interest(self, t_start, t_end, trade_exchange):
        """计算并从现金中扣除当日融资融券利息 (按 365 自然日)"""
        import pandas as pd

        current_date = pd.Timestamp(t_start)

        # 计算距离上次计息过去了多少自然日
        if self._last_interest_date is None:
            days_diff = 1  # 首日按 1 天计
        else:
            days_diff = (current_date - self._last_interest_date).days

        self._last_interest_date = current_date

        if days_diff <= 0:
            return

        # A. 计算融券负债 (空头市值)
        short_debt_value = 0.0
        for stock_id, info in self.current_position._iter_position_items():
            amount = info.get("amount", 0)
            if amount < 0:
                try:
                    # 使用当日收盘价计算真实负债
                    price = trade_exchange.get_close(stock_id, t_start, t_end)
                    short_debt_value += abs(amount) * price
                except Exception:
                    short_debt_value += abs(amount) * info.get("price", 0)

        # B. 计算融资负债 (可用现金为负的部分)
        cash_debt = max(0.0, -self.current_position.position.get("cash", 0.0))

        total_debt = short_debt_value + cash_debt

        if total_debt > 0:
            interest_charge = (
                short_debt_value * self.borrow_rate
                + cash_debt * self.financing_rate
            ) * days_diff / 365

            # 直接从现金账户扣除
            self.current_position.position["cash"] -= interest_charge

            if interest_charge > 100:
                task_logger.debug(
                    "interest_charged",
                    "计息扣除",
                    interest_charge=round(interest_charge, 2),
                    total_debt=round(total_debt, 2),
                    days_diff=days_diff,
                )

    def _update_state_from_order(self, order, trade_val: float, cost: float, trade_price: float) -> None:
        """修正 Qlib 内置的 PnL 统计逻辑，确保多空对冲下的收益率计算正确"""
        if not self.is_port_metr_enabled():
            return

        self.accum_info.add_turnover(trade_val)
        self.accum_info.add_cost(cost)

        pre_amount, pre_price = self._pre_trade_snapshot or (0.0, trade_price)
        trade_amount = trade_val / trade_price if trade_price else 0.0

        # 这里的 profit 主要用于 accum_info 的统计展示，不影响账户实际 cash
        if order.direction == order.SELL:
            close_amount = min(max(pre_amount, 0.0), trade_amount)
            if close_amount > 0:
                profit = (trade_price - pre_price) * close_amount
            else:
                # 融券开空：此时尚未实现盈亏
                profit = 0.0
            self.accum_info.add_return_value(profit)
        elif order.direction == order.BUY:
            cover_amount = min(max(-pre_amount, 0.0), trade_amount)
            if cover_amount > 0:
                profit = (pre_price - trade_price) * cover_amount
            else:
                # 买入多头：此时尚未实现盈亏
                profit = 0.0
            self.accum_info.add_return_value(profit)


def ensure_margin_backtest_support() -> str:
    """注册 MarginPosition 并 Patch Qlib 账户工厂"""
    import qlib.backtest as qlib_backtest_module
    import qlib.backtest.position as qlib_position_module

    # 动态注册类
    qlib_position_module.MarginPosition = MarginPosition

    if getattr(qlib_backtest_module, "_quantmind_margin_account_patched_v2", False):
        return "MarginPosition"

    original_create_account_instance = qlib_backtest_module.create_account_instance

    def _create_account_instance_with_margin_v2(start_time, end_time, benchmark, account, pos_type="Position"):
        if pos_type != "MarginPosition":
            return original_create_account_instance(
                start_time=start_time,
                end_time=end_time,
                benchmark=benchmark,
                account=account,
                pos_type=pos_type,
            )

        # 初始化参数解析
        if isinstance(account, (int, float)):
            init_cash = account
            position_dict = {}
        elif isinstance(account, dict):
            position_dict = account.copy()
            init_cash = position_dict.pop("cash")
        else:
            raise ValueError("account must be int, float or dict")

        # 返回自定义的 MarginAccount
        financing_rate = position_dict.pop("_qm_financing_rate", 0.08)
        borrow_rate = position_dict.pop("_qm_borrow_rate", 0.08)
        return MarginAccount(
            init_cash=init_cash,
            position_dict=position_dict,
            pos_type=pos_type,
            financing_rate=financing_rate,
            borrow_rate=borrow_rate,
            benchmark_config=(
                {}
                if benchmark is None
                else {
                    "benchmark": benchmark,
                    "start_time": start_time,
                    "end_time": end_time,
                }
            ),
        )

    qlib_backtest_module.create_account_instance = _create_account_instance_with_margin_v2
    qlib_backtest_module._quantmind_margin_account_patched_v2 = True
    return "MarginPosition"
