"""
投资组合管理模块
"""

from datetime import datetime
from typing import Dict


class Position:
    """持仓类"""

    def __init__(self, symbol: str):
        """
        初始化持仓

        Args:
            symbol: 交易标的
        """
        self.symbol = symbol
        self.quantity = 0.0
        # T+1 账本模型（记录在案项收口）：可卖量——当日买入次日才解锁；
        # 决策/撮合与模拟侧 lua 锁定同一语义（同日买→卖被拒，不是静默 T+0）
        self.available_quantity = 0.0
        self.avg_cost = 0.0
        self.market_value = 0.0
        self.unrealized_pnl = 0.0
        self.realized_pnl = 0.0
        self.last_price = 0.0
        self.updated_at = datetime.now()

    def update_market_value(self, price: float) -> None:
        """更新市值（支持多头和空头）"""
        self.last_price = price
        self.market_value = self.quantity * price
        if self.quantity > 0:
            self.unrealized_pnl = (price - self.avg_cost) * self.quantity
        elif self.quantity < 0:
            # 空头：价格下跌为盈利，价格上涨为亏损
            self.unrealized_pnl = (self.avg_cost - price) * abs(self.quantity)
        else:
            self.unrealized_pnl = 0.0
        self.updated_at = datetime.now()

    def add_shares(self, quantity: float, cost: float) -> None:
        """增加多头持仓（quantity 必须为正）"""
        if self.quantity == 0:
            self.avg_cost = cost
            self.quantity += quantity
        else:
            total_cost = self.quantity * self.avg_cost + quantity * cost
            self.quantity += quantity
            self.avg_cost = total_cost / self.quantity

    def open_short(self, quantity: float, price: float) -> None:
        """开空仓（quantity 为正数，内部存为负数）"""
        if self.quantity == 0:
            self.avg_cost = price
            self.quantity = -quantity
        elif self.quantity < 0:
            # 加仓空头：更新平均开仓价
            total_short = abs(self.quantity) + quantity
            self.avg_cost = (abs(self.quantity) * self.avg_cost + quantity * price) / total_short
            self.quantity -= quantity
        else:
            raise ValueError("持仓已为多头，不可直接开空，请先平多")

    def cover_short(self, quantity: float, price: float) -> float:
        """平空仓，返回实现盈亏（quantity 为正数）"""
        if self.quantity >= 0:
            raise ValueError("无空头持仓可平")
        if quantity > abs(self.quantity):
            raise ValueError("平空数量超过持仓")

        # 空头盈亏 = (开仓价 - 平仓价) * 数量
        realized_pnl = (self.avg_cost - price) * quantity
        self.realized_pnl += realized_pnl
        self.quantity += quantity  # quantity < 0，加正数变小

        if self.quantity == 0:
            self.avg_cost = 0.0
            self.unrealized_pnl = 0.0

        return realized_pnl

    def unlock_t1(self) -> None:
        """日初解锁：昨日及更早买入的持仓转为可卖（A 股 T+1）。"""
        self.available_quantity = self.quantity

    def reduce_shares(self, quantity: float, price: float) -> float:
        """减少持仓，返回实现盈亏（调用方须先过 available 校验，见 Portfolio.sell）"""
        if quantity > self.quantity:
            raise ValueError("卖出数量超过持仓")
        if quantity > self.available_quantity:
            raise ValueError(
                f"T+1 不可卖：{self.symbol} 可卖 {self.available_quantity} < 卖出 {quantity}"
                "（当日买入次日才可卖）"
            )
        self.available_quantity -= quantity

            # 计算实现盈亏
        realized_pnl = (price - self.avg_cost) * quantity
        self.realized_pnl += realized_pnl

        # 更新持仓
        self.quantity -= quantity

        # 如果全部卖出，重置平均成本
        if self.quantity == 0:
            self.avg_cost = 0.0
            self.unrealized_pnl = 0.0

        return realized_pnl


class Portfolio:
    """投资组合类"""

    def __init__(self, initial_cash: float = 100000.0):
        """
        初始化投资组合

        Args:
            initial_cash: 初始现金
        """
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.positions: dict[str, Position] = {}
        self.total_value = initial_cash
        self.last_total_value = initial_cash
        self.updated_at = datetime.now()

    def reset(self) -> None:
        """重置投资组合"""
        self.cash = self.initial_cash
        self.positions.clear()
        self.total_value = self.initial_cash
        self.last_total_value = self.initial_cash
        self.updated_at = datetime.now()

    def short_sell(self, symbol: str, quantity: float, price: float, commission: float = 0.0) -> None:
        """融券开空：扣除手续费，冻结做空所得资金"""
        # 只扣手续费，卖出所得不增加可用现金（A股融券规则）
        if commission > self.cash:
            raise ValueError(f"现金不足以支付融券手续费，需要 {commission}，可用 {self.cash}")
        self.cash -= commission

        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol)
        self.positions[symbol].open_short(quantity, price)
        self.positions[symbol].update_market_value(price)
        self._update_total_value()

    def buy_to_cover(self, symbol: str, quantity: float, price: float, commission: float = 0.0) -> float:
        """买入平空：支付平仓成本，返回实现盈亏"""
        if symbol not in self.positions or self.positions[symbol].quantity >= 0:
            raise ValueError(f"无空头持仓可平: {symbol}")

        position = self.positions[symbol]
        realized_pnl = position.cover_short(quantity, price)

        # 平空成本 = 买入金额 + 手续费；实现盈亏已算入 realized_pnl
        cover_cost = quantity * price + commission
        self.cash -= cover_cost

        if position.quantity == 0:
            del self.positions[symbol]
        else:
            position.update_market_value(price)

        self._update_total_value()
        return realized_pnl

    def buy(self, symbol: str, quantity: float, price: float, commission: float = 0.0) -> None:
        """买入"""
        total_cost = quantity * price + commission
        if total_cost > self.cash:
            raise ValueError(f"现金不足，需要 {total_cost}，可用 {self.cash}")

        # 更新现金
        self.cash -= total_cost

        # 更新持仓
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol)

        self.positions[symbol].add_shares(quantity, price)
        self.positions[symbol].update_market_value(price)

        # 更新总价值
        self._update_total_value()

    def sell(self, symbol: str, quantity: float, price: float, commission: float = 0.0) -> float:
        """卖出（T+1：可卖量=昨日及更早持仓；当日买入当日卖出被拒）"""
        if symbol not in self.positions or self.positions[symbol].quantity < quantity:
            raise ValueError(f"持仓不足，{symbol} 持仓 {self.positions.get(symbol, Position(symbol)).quantity}")
        if self.positions[symbol].available_quantity < quantity:
            raise ValueError(
                f"T+1 不可卖：{symbol} 可卖 {self.positions[symbol].available_quantity} < 卖出 {quantity}"
                "（当日买入次日才可卖）"
            )

            # 更新持仓
        position = self.positions[symbol]
        realized_pnl = position.reduce_shares(quantity, price)

        # 计算收入
        total_proceeds = quantity * price - commission

        # 更新现金
        self.cash += total_proceeds

        # 如果持仓为0，删除
        if position.quantity == 0:
            del self.positions[symbol]
        else:
            position.update_market_value(price)

            # 更新总价值
        self._update_total_value()
        return realized_pnl

    def unlock_t1(self) -> None:
        """日初解锁全部持仓（引擎每日循环起始调用，先于当日成交处理）。"""
        for position in self.positions.values():
            position.unlock_t1()

    def update_market_value(self, date: datetime, prices: dict[str, float]) -> None:
        """更新所有持仓的市值，支持多标的"""
        self.last_total_value = self.total_value

        for symbol, position in self.positions.items():
            if symbol in prices:
                position.update_market_value(prices[symbol])

        self._update_total_value()

    def _update_total_value(self) -> None:
        """更新总价值"""
        positions_value = sum(pos.market_value for pos in self.positions.values())
        self.total_value = self.cash + positions_value
        self.updated_at = datetime.now()

    def get_total_value(self) -> float:
        """获取总价值"""
        return self.total_value

    def get_positions_value(self) -> float:
        """获取持仓市值"""
        return sum(pos.market_value for pos in self.positions.values())

    def get_positions(self) -> dict[str, dict]:
        """获取持仓信息"""
        return {
            symbol: {
                "quantity": pos.quantity,
                "available_quantity": pos.available_quantity,
                "avg_cost": pos.avg_cost,
                "market_value": pos.market_value,
                "unrealized_pnl": pos.unrealized_pnl,
                "realized_pnl": pos.realized_pnl,
                "last_price": pos.last_price,
            }
            for symbol, pos in self.positions.items()
        }

    def get_daily_return(self) -> float:
        """获取日收益率"""
        if self.last_total_value == 0:
            return 0.0
        return (self.total_value - self.last_total_value) / self.last_total_value

    def get_position(self, symbol: str) -> float:
        """获取指定标的持仓数量"""
        return self.positions.get(symbol, Position(symbol)).quantity
