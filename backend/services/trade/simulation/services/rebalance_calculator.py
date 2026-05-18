"""
Rebalance Calculator - 调仓计算器
支持 TopK 筛选、多种权重模式、涨跌停过滤、先卖后买逻辑
"""

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from backend.services.trade.simulation.services.signal_loader import SignalScore

logger = logging.getLogger(__name__)


class WeightMode(str, Enum):
    """权重模式"""
    EQUAL = "equal"  # 等权
    SCORE_WEIGHTED = "score_weighted"  # 按 score 加权
    CUSTOM = "custom"  # 自定义权重


@dataclass
class StrategyConfig:
    """策略配置"""
    topk: int = 10
    weight_mode: WeightMode = WeightMode.EQUAL
    custom_weights: dict[str, float] = field(default_factory=dict)
    min_score: float = 0.0
    max_position_pct: float = 0.15  # 单只股票最大仓位比例
    lot_size: int = 100  # 最小交易单位


@dataclass
class Quote:
    """行情数据"""
    symbol: str
    current_price: float
    is_limit_up: bool = False
    is_limit_down: bool = False
    is_suspended: bool = False
    pre_close: float | None = None


@dataclass
class Order:
    """交易指令"""
    symbol: str
    side: str  # "BUY" | "SELL"
    quantity: int
    price: float
    reason: str = ""


@dataclass
class SimulationAccount:
    """模拟账户快照"""
    cash: float
    total_asset: float
    positions: dict[str, dict[str, Any]]


class RebalanceCalculator:
    """
    调仓计算器：
    1. 根据 signal score 排序，取 TopK
    2. 计算目标权重（支持多种模式）
    3. 计算目标持仓金额 → 目标股数
    4. 剔除涨跌停标的
    5. 计算买卖指令（先卖后买）
    """

    def calculate(
        self,
        signals: list[SignalScore],
        strategy: StrategyConfig,
        quotes: dict[str, Quote],
        account: SimulationAccount,
    ) -> list[Order]:
        """
        计算调仓指令。

        Args:
            signals: 信号列表
            strategy: 策略配置
            quotes: 行情数据 {symbol: Quote}
            account: 当前账户状态

        Returns:
            交易指令列表（先卖后买）
        """
        if not signals:
            logger.info("RebalanceCalculator: 无信号，跳过调仓")
            return []

        # 1. 剔除涨跌停、停牌标的
        tradable_signals = self._filter_tradable(signals, quotes)
        if not tradable_signals:
            logger.info("RebalanceCalculator: 无可交易标的，跳过调仓")
            return []

        # 2. TopK 筛选
        topk_signals = sorted(
            tradable_signals,
            key=lambda x: x.score,
            reverse=True,
        )[: strategy.topk]

        # 3. 计算目标权重
        weights = self._calc_weights(topk_signals, strategy)

        # 4. 计算目标持仓
        target_positions = self._calc_target_positions(
            topk_signals, weights, quotes, account, strategy
        )

        # 5. 生成调仓指令（先卖后买）
        orders = self._generate_orders(account.positions, target_positions, quotes)

        logger.info(
            "RebalanceCalculator: 生成 %d 条指令, 卖出=%d 买入=%d",
            len(orders),
            sum(1 for o in orders if o.side == "SELL"),
            sum(1 for o in orders if o.side == "BUY"),
        )
        return orders

    def _filter_tradable(
        self,
        signals: list[SignalScore],
        quotes: dict[str, Quote],
    ) -> list[SignalScore]:
        """剔除涨跌停、停牌标的"""
        tradable = []
        for sig in signals:
            quote = quotes.get(sig.symbol)
            if not quote:
                logger.debug("RebalanceCalculator: %s 无行情数据，跳过", sig.symbol)
                continue
            if quote.is_suspended:
                logger.debug("RebalanceCalculator: %s 停牌，跳过", sig.symbol)
                continue
            if quote.is_limit_up or quote.is_limit_down:
                logger.debug(
                    "RebalanceCalculator: %s 涨跌停（up=%s down=%s），跳过",
                    sig.symbol,
                    quote.is_limit_up,
                    quote.is_limit_down,
                )
                continue
            if quote.current_price <= 0:
                logger.debug("RebalanceCalculator: %s 价格无效，跳过", sig.symbol)
                continue
            tradable.append(sig)
        return tradable

    def _calc_weights(
        self,
        signals: list[SignalScore],
        strategy: StrategyConfig,
    ) -> dict[str, float]:
        """计算目标权重"""
        if not signals:
            return {}

        if strategy.weight_mode == WeightMode.CUSTOM and strategy.custom_weights:
            return strategy.custom_weights

        if strategy.weight_mode == WeightMode.EQUAL:
            weight = 1.0 / len(signals)
            return {sig.symbol: weight for sig in signals}

        if strategy.weight_mode == WeightMode.SCORE_WEIGHTED:
            total_score = sum(sig.score for sig in signals)
            if total_score <= 0:
                weight = 1.0 / len(signals)
                return {sig.symbol: weight for sig in signals}
            return {
                sig.symbol: sig.score / total_score
                for sig in signals
            }

        # 默认等权
        weight = 1.0 / len(signals)
        return {sig.symbol: weight for sig in signals}

    def _calc_target_positions(
        self,
        signals: list[SignalScore],
        weights: dict[str, float],
        quotes: dict[str, Quote],
        account: SimulationAccount,
        strategy: StrategyConfig,
    ) -> dict[str, int]:
        """计算目标持仓股数"""
        target_positions = {}
        total_asset = account.total_asset

        for sig in signals:
            weight = weights.get(sig.symbol, 0)
            if weight <= 0:
                continue

            # 单只股票最大仓位限制
            effective_weight = min(weight, strategy.max_position_pct)
            target_value = total_asset * effective_weight

            quote = quotes.get(sig.symbol)
            if not quote or quote.current_price <= 0:
                continue

            # 计算目标股数（向下取整到整手）
            raw_quantity = target_value / quote.current_price
            lot_quantity = self._floor_to_lot(raw_quantity, strategy.lot_size)

            if lot_quantity > 0:
                target_positions[sig.symbol] = lot_quantity

        return target_positions

    def _floor_to_lot(self, quantity: float, lot_size: int = 100) -> int:
        """向下取整到整手"""
        if quantity <= 0:
            return 0
        return int(quantity // lot_size) * lot_size

    def _generate_orders(
        self,
        current_positions: dict[str, dict[str, Any]],
        target_positions: dict[str, int],
        quotes: dict[str, Quote],
    ) -> list[Order]:
        """
        生成调仓指令（先卖后买）。

        逻辑：
        1. 先计算需要卖出的股票（当前持有但不在目标中，或目标数量小于当前）
        2. 再计算需要买入的股票（目标中有但当前没有，或目标数量大于当前）
        """
        sell_orders: list[Order] = []
        buy_orders: list[Order] = []

        current_symbols = set(current_positions.keys())
        target_symbols = set(target_positions.keys())

        # 需要卖出的股票
        for symbol in current_symbols:
            current_pos = current_positions.get(symbol, {})
            current_qty = int(float(current_pos.get("volume", 0) or 0))
            target_qty = target_positions.get(symbol, 0)

            if current_qty > target_qty:
                sell_qty = current_qty - target_qty
                quote = quotes.get(symbol)
                price = quote.current_price if quote else 0

                if sell_qty > 0 and price > 0:
                    sell_orders.append(Order(
                        symbol=symbol,
                        side="SELL",
                        quantity=sell_qty,
                        price=price,
                        reason=f"调仓卖出: 当前{current_qty} → 目标{target_qty}",
                    ))

        # 需要买入的股票
        for symbol in target_symbols:
            target_qty = target_positions.get(symbol, 0)
            current_pos = current_positions.get(symbol, {})
            current_qty = int(float(current_pos.get("volume", 0) or 0))

            if target_qty > current_qty:
                buy_qty = target_qty - current_qty
                quote = quotes.get(symbol)
                price = quote.current_price if quote else 0

                if buy_qty > 0 and price > 0:
                    buy_orders.append(Order(
                        symbol=symbol,
                        side="BUY",
                        quantity=buy_qty,
                        price=price,
                        reason=f"调仓买入: 当前{current_qty} → 目标{target_qty}",
                    ))

        # 先卖后买
        return sell_orders + buy_orders


rebalance_calculator = RebalanceCalculator()
