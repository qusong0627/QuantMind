from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from enum import IntEnum
import datetime


class OrderStatus(IntEnum):
    """
    ### 订单状态枚举  (IntEnum)
    - Created = 0      # 订单已被创建
    - Submitted = 1    # 订单已被传递给经纪商 Broker
    - Accepted = 2     # 订单已被经纪商接收
    - Partial = 3      # 订单已被部分成交
    - Completed = 4    # 订单已成交
    - Canceled = 5     # 订单已被撤销
    - Expired = 6      # 订单已到期
    - Margin = 7       # 保证金不足
    - Rejected = 8     # 订单已被拒绝
    """
    Created = 0      # 订单已被创建
    Submitted = 1    # 订单已被传递给经纪商 Broker
    Accepted = 2     # 订单已被经纪商接收
    Partial = 3      # 订单已被部分成交
    Completed = 4    # 订单已成交
    Canceled = 5     # 订单已被撤销
    Expired = 6      # 订单已到期
    Margin = 7       # 保证金不足
    Rejected = 8     # 订单已被拒绝

    @classmethod
    def get_name(cls, status: int) -> str:
        """获取状态名称"""
        status_names = [
            'Created', 'Submitted', 'Accepted', 'Partial', 'Completed',
            'Canceled', 'Expired', 'Margin', 'Rejected'
        ]
        return status_names[status]


class OrderType(IntEnum):
    """
    ### 订单类型枚举  (IntEnum)
    - Market = 0       # 市价单（开盘价执行）
    - Close = 1        # 收盘价单（收盘价执行）
    - Limit = 2        # 限价单
    - Stop = 3         # 止损单
    - StopLimit = 4    # 止损限价单
    """
    Market = 0       # 市价单（开盘价执行）
    Close = 1        # 收盘价单（收盘价执行）
    Limit = 2        # 限价单
    Stop = 3         # 止损单
    StopLimit = 4    # 止损限价单

    @classmethod
    def get_name(cls, exectype: int) -> str:
        """### 获取类型名称"""
        type_names = ['Market', 'Close', 'Limit', 'Stop', 'StopLimit']
        return type_names[exectype]

    @classmethod
    def is_immediate(cls, exectype: int) -> bool:
        """### 判断是否为即时订单（Market或Close）"""
        return exectype in (cls.Market, cls.Close)

    @classmethod
    def is_conditional(cls, exectype: int) -> bool:
        """### 判断是否为条件订单（Limit, Stop, StopLimit）"""
        return exectype in (cls.Limit, cls.Stop, cls.StopLimit)

    @classmethod
    def is_limit_related(cls, exectype: int) -> bool:
        """### 判断是否为限价相关订单（Limit, StopLimit）"""
        return exectype in (cls.Limit, cls.StopLimit)

    @classmethod
    def requires_price(cls, exectype: int) -> bool:
        """### 判断该订单类型是否需要价格参数"""
        return exectype in (cls.Limit, cls.Stop, cls.StopLimit)

    @classmethod
    def requires_bar_specification(cls, exectype: int) -> bool:
        """### 判断该订单类型是否需要bar参数指定执行时间"""
        # Market和Close需要指定bar参数
        return exectype in (cls.Market, cls.Close)


class OrderSide(IntEnum):
    """
    ### 订单方向 (IntEnum)
    - Buy = 0
    - Sell = 1"""
    Buy = 0
    Sell = 1

    @classmethod
    def get_name(cls, side: int) -> str:
        return 'Buy' if side == cls.Buy else 'Sell'


@dataclass
class Order:
    """
    ### 订单类
    #### 基础信息
    - create_time: Optional[datetime.datetime]
    - side: OrderSide  # 买入/卖出
    - size: int  # 手数（正数）

    #### 订单类型和价格
    - exectype: OrderType = OrderType.Market
    - price: Optional[float] = None  # 委托价格（限价单/止损单）
    - pricelimit: Optional[float] = None  # 限价（止损限价单）

    #### 有效期
    - valid: Optional[Union[datetime.datetime, datetime.timedelta, int]] = None

    #### OCO订单（One Cancel Others）
    - oco: Optional['Order'] = None

    #### 订单状态和标识
    - status: OrderStatus = OrderStatus.Created
    - ref: int = 0  # 订单编号
    - bar: int = 1  # 后一根bar
    - tradeid: int = 0
    - transmitted: bool = True

    #### 执行结果
    - executed_price: Optional[float] = None
    - executed_size: int = 0
    - executed_datetime: Optional[datetime.datetime] = None
    - executed_value: float = 0.0
    - executed_commission: float = 0.0
    """
    # 基础信息
    create_time: datetime.datetime | None
    side: OrderSide  # 买入/卖出
    size: int  # 手数（正数）

    # 订单类型和价格
    exectype: OrderType = OrderType.Market
    price: float | None = None  # 委托价格（限价单/止损单）
    pricelimit: float | None = None  # 限价（止损限价单）

    # 有效期
    valid: datetime.datetime | datetime.timedelta | int | None= None

    # OCO订单（One Cancel Others）
    oco: Order | None = None

    # 订单状态和标识
    status: OrderStatus = OrderStatus.Created
    ref: int = 0  # 订单编号
    bar: int = 1  # 后一根bar
    tradeid: int = 0
    transmitted: bool = True

    # 执行结果
    executed_price: float | None = None
    executed_size: int = 0
    executed_datetime: datetime.datetime | None = None
    executed_value: float = 0.0
    executed_commission: float = 0.0

    # 自定义信息
    info: dict[str, Any] = None

    def __post_init__(self):
        if self.info is None:
            self.info = {}
        self._isnumvalid: bool = isinstance(self.valid, int)

    @property
    def is_buy(self) -> bool:
        """### 是否为买入订单"""
        return self.side == OrderSide.Buy

    @property
    def is_sell(self) -> bool:
        """### 是否为卖出订单"""
        return self.side == OrderSide.Sell

    @property
    def remaining(self) -> int:
        """### 剩余未成交手数"""
        return self.size - self.executed_size

    @property
    def is_completed(self) -> bool:
        """### 是否已完成"""
        return self.status == OrderStatus.Completed

    @property
    def is_canceled(self) -> bool:
        """### 是否已取消"""
        return self.status == OrderStatus.Canceled

    @property
    def is_active(self) -> bool:
        """### 是否活跃（可成交）"""
        return self.status in [
            OrderStatus.Created,
            OrderStatus.Submitted,
            OrderStatus.Accepted,
            OrderStatus.Partial
        ]

    def update_status(self, new_status: OrderStatus):
        """### 更新订单状态"""
        self.status = new_status

    def execute(self, price: float, size: int, datetime: datetime.datetime,
                value: float = 0.0, commission: float = 0.0):
        """### 执行订单"""
        self.executed_price = price
        self.executed_size = size
        self.executed_datetime = datetime
        self.executed_value = value
        self.executed_commission = commission

        if self.remaining <= 0:
            self.status = OrderStatus.Completed
        else:
            self.status = OrderStatus.Partial

    def cancel(self):
        """### 取消订单"""
        self.status = OrderStatus.Canceled

    def reject(self):
        """### 拒绝订单"""
        self.status = OrderStatus.Rejected

    def to_dict(self) -> dict[str, Any]:
        """### 转换为字典"""
        return {
            'ref': self.ref,
            'create_time': self.create_time,
            'side': OrderSide.get_name(self.side),
            'size': self.size,
            'exectype': OrderType.get_name(self.exectype),
            'price': self.price,
            'pricelimit': self.pricelimit,
            'status': OrderStatus.get_name(self.status),
            'executed_price': self.executed_price,
            'executed_size': self.executed_size,
            'remaining': self.remaining
        }

    # 新增方法
    def get_remaining_valid_periods(self) -> int | None:
        """### 获取剩余有效周期数"""
        if isinstance(self.valid, int):
            return self.valid
        return None

    def get_expiry_time(self) -> datetime.datetime | None:
        """### 获取过期时间"""
        if isinstance(self.valid, datetime.datetime):
            return self.valid
        elif isinstance(self.valid, datetime.timedelta) and self.create_time:
            return self.create_time + self.valid
        return None
