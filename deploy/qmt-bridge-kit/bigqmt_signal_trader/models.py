"""交易信号、委托请求和账户快照的数据模型。"""

import datetime as _dt
from enum import Enum
from typing import Any, Dict, List, Optional


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    CLEAR = "CLEAR"
    CANCEL = "CANCEL"


class SignalStatus(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    SUBMITTED = "SUBMITTED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    FILLED = "FILLED"


def parse_datetime(value: Any, field_name: str) -> _dt.datetime:
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return _dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise ValueError(f"{field_name} must use format YYYY-MM-DD HH:MM:SS") from exc
    raise ValueError(f"{field_name} is required")


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in ("1", "true", "yes", "y", "on")


class TradeSignal:
    def __init__(
        self,
        signal_id,
        account_id,
        action,
        created_at,
        expire_at,
        schema_version,
        stock_code="",
        stock_name="",
        amount=None,
        percentage=None,
        price_type="AUTO_LIMIT",
        price=None,
        strategy_name="bigqmt_signal_trader",
        remark="",
        source="",
        source_type="auto",
        force=False,
        bypass_stop_buy=False,
        bypass_stop_sell=False,
        bypass_daily_limit=False,
        status=SignalStatus.PENDING,
        raw_payload=None,
    ):
        self.signal_id = signal_id
        self.account_id = account_id
        self.action = action
        self.created_at = created_at
        self.expire_at = expire_at
        self.schema_version = schema_version
        self.stock_code = stock_code
        self.stock_name = stock_name
        self.amount = amount
        self.percentage = percentage
        self.price_type = price_type
        self.price = price
        self.strategy_name = strategy_name
        self.remark = remark
        self.source = source
        self.source_type = source_type
        self.force = force
        self.bypass_stop_buy = bypass_stop_buy
        self.bypass_stop_sell = bypass_stop_sell
        self.bypass_daily_limit = bypass_daily_limit
        self.status = status
        self.raw_payload = dict(raw_payload or {})

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "TradeSignal":
        required = ("signal_id", "account_id", "action", "created_at", "expire_at", "schema_version")
        for field_name in required:
            if payload.get(field_name) in (None, ""):
                raise ValueError(f"{field_name} is required")

        try:
            action = SignalAction(str(payload["action"]).upper())
        except ValueError as exc:
            raise ValueError(f"unsupported action: {payload.get('action')}") from exc

        amount = _optional_int(payload.get("amount"))
        percentage = _optional_float(payload.get("percentage"))
        stock_code = str(payload.get("stock_code") or "").strip().upper()

        if action == SignalAction.BUY:
            if not stock_code:
                raise ValueError("stock_code is required for BUY")
            if amount is None or amount <= 0:
                raise ValueError("amount must be positive for BUY")
        elif action == SignalAction.SELL:
            if not stock_code:
                raise ValueError("stock_code is required for SELL")
            if amount is None and percentage is None:
                raise ValueError("amount or percentage is required for SELL")
        elif action == SignalAction.CLEAR and percentage is None:
            percentage = 100.0

        return cls(
            signal_id=str(payload["signal_id"]),
            account_id=str(payload["account_id"]),
            action=action,
            stock_code=stock_code,
            stock_name=str(payload.get("stock_name") or ""),
            amount=amount,
            percentage=percentage,
            price_type=str(payload.get("price_type") or "AUTO_LIMIT").upper(),
            price=_optional_float(payload.get("price")),
            strategy_name=str(payload.get("strategy_name") or "bigqmt_signal_trader"),
            remark=str(payload.get("remark") or ""),
            source=str(payload.get("source") or ""),
            source_type=str(payload.get("source_type") or "auto"),
            force=_bool_value(payload.get("force", False)),
            bypass_stop_buy=_bool_value(payload.get("bypass_stop_buy", False)),
            bypass_stop_sell=_bool_value(payload.get("bypass_stop_sell", False)),
            bypass_daily_limit=_bool_value(payload.get("bypass_daily_limit", False)),
            created_at=parse_datetime(payload.get("created_at"), "created_at"),
            expire_at=parse_datetime(payload.get("expire_at"), "expire_at"),
            schema_version=int(payload["schema_version"]),
            raw_payload=dict(payload),
        )

    def is_expired(self, now: _dt.datetime) -> bool:
        return now > self.expire_at


class PositionSnapshot:
    def __init__(
        self,
        stock_code,
        volume,
        available,
        cost=0.0,
        stock_name="",
        market_value=None,
        price=None,
        open_price=None,
        frozen_volume=0,
        on_road_volume=0,
        yesterday_volume=None,
        direction=48,
    ):
        self.stock_code = stock_code
        self.volume = volume
        self.available = available
        self.cost = cost
        self.stock_name = stock_name
        self.market_value = market_value
        self.price = price
        self.open_price = open_price
        self.frozen_volume = frozen_volume
        self.on_road_volume = on_road_volume
        self.yesterday_volume = yesterday_volume
        self.direction = direction


class PositionStatisticsSnapshot:
    """One position-statistics row, i.e. ``get_trade_detail_data(..., "POSITION_STATISTICS")``.

    """

    def __init__(
        self,
        account_id="",
        exchange_id="",
        exchange_name="",
        product_id="",
        instrument_id="",
        instrument_name="",
        stock_code="",
        direction=0,
        hedge_flag=0,
        position=0,
        yesterday_position=0,
        today_position=0,
        can_close_vol=0,
        position_cost=None,
        avg_price=None,
        position_profit=None,
        float_profit=None,
        open_price=None,
        used_margin=None,
        used_commission=None,
        frozen_margin=None,
        frozen_commission=None,
        instrument_value=None,
        open_times=0,
        open_volume=0,
        cancel_times=0,
        last_price=None,
        rise_ratio=None,
        product_name="",
        royalty=None,
        expire_date="",
        assest_weight=None,
        increase_by_settlement=None,
        margin_ratio=None,
        float_profit_divide_by_used_margin=None,
        float_profit_divide_by_balance=None,
        today_profit_loss=None,
        yesterday_init_position=0,
        frozen_royalty=None,
        today_close_profit_loss=None,
        close_profit=None,
        ft_product_name="",
        open_cost=None,
    ):
        self.account_id = account_id
        self.exchange_id = exchange_id
        self.exchange_name = exchange_name
        self.product_id = product_id
        self.instrument_id = instrument_id
        self.instrument_name = instrument_name
        self.stock_code = stock_code
        self.direction = direction
        self.hedge_flag = hedge_flag
        self.position = position
        self.yesterday_position = yesterday_position
        self.today_position = today_position
        self.can_close_vol = can_close_vol
        self.position_cost = position_cost
        self.avg_price = avg_price
        self.position_profit = position_profit
        self.float_profit = float_profit
        self.open_price = open_price
        self.used_margin = used_margin
        self.used_commission = used_commission
        self.frozen_margin = frozen_margin
        self.frozen_commission = frozen_commission
        self.instrument_value = instrument_value
        self.open_times = open_times
        self.open_volume = open_volume
        self.cancel_times = cancel_times
        self.last_price = last_price
        self.rise_ratio = rise_ratio
        self.product_name = product_name
        self.royalty = royalty
        self.expire_date = expire_date
        self.assest_weight = assest_weight
        self.increase_by_settlement = increase_by_settlement
        self.margin_ratio = margin_ratio
        self.float_profit_divide_by_used_margin = float_profit_divide_by_used_margin
        self.float_profit_divide_by_balance = float_profit_divide_by_balance
        self.today_profit_loss = today_profit_loss
        self.yesterday_init_position = yesterday_init_position
        self.frozen_royalty = frozen_royalty
        self.today_close_profit_loss = today_close_profit_loss
        self.close_profit = close_profit
        self.ft_product_name = ft_product_name
        self.open_cost = open_cost


class AssetSnapshot:
    """Account funds, mirroring MiniQMT's ``XtAsset``.

    Field names follow ``xtquant.xttype.XtAsset(account_id, cash, frozen_cash,
    market_value, total_asset)`` so ``query_stock_asset`` can hand callers the
    same attributes they get from MiniQMT.

    ``cash`` is 可用 (available), NOT the full 资金余额:
    ``total_asset == cash + frozen_cash + market_value``. New fields are
    appended with None defaults so existing positional callers keep working,
    and None means "the terminal did not report it" — distinct from 0.0.
    """

    def __init__(self, account_id, cash=None, total_asset=None, frozen_cash=None, market_value=None):
        self.account_id = account_id
        self.cash = cash
        self.total_asset = total_asset
        self.frozen_cash = frozen_cash
        self.market_value = market_value


class AccountSnapshot:
    def __init__(self, account_id, asset, positions, reason, updated_at):
        self.account_id = account_id
        self.asset = asset
        self.positions = positions
        self.reason = reason
        self.updated_at = updated_at


class OrderRequest:
    def __init__(
        self,
        signal_id,
        account_id,
        action,
        stock_code,
        volume,
        price,
        price_type,
        strategy_name,
        remark="",
        order_type=None,
    ):
        # MiniQMT-style order_type (xtconstant). Only set for operations a
        # BUY/SELL action cannot express -- credit financing, repayment and the
        # special-margin family. None means an ordinary stock order.
        self.order_type = order_type
        self.signal_id = signal_id
        self.account_id = account_id
        self.action = action
        self.stock_code = stock_code
        self.volume = volume
        self.price = price
        self.price_type = price_type
        self.strategy_name = strategy_name
        self.remark = remark


class OrderSubmitResult:
    def __init__(self, status, user_order_id, order_sys_id=None, message=""):
        self.status = status
        self.user_order_id = user_order_id
        self.order_sys_id = order_sys_id
        self.message = message


class OrderSnapshot:
    def __init__(
        self,
        order_sys_id,
        user_order_id,
        stock_code,
        action,
        volume,
        traded_volume,
        status,
        price=0.0,
        strategy_name="",
        remark="",
        order_time=0,
        status_msg="",
        traded_price=0.0,
        price_type=None,
        account_type=0,
        instrument_name="",
        secu_account="",
        offset_flag=None,
        direction=None,
        trade_amount=0.0,
    ):
        self.order_sys_id = order_sys_id
        self.user_order_id = user_order_id
        self.stock_code = stock_code
        self.action = action
        self.volume = volume
        self.traded_volume = traded_volume
        self.status = status
        self.price = price
        self.traded_price = traded_price
        self.strategy_name = strategy_name
        self.remark = remark
        # 报单时间, Unix 秒 -- MiniQMT XtOrder.order_time 的语义。0 = 未上报。
        # 追加在末尾并给默认值, 保持既有位置参数调用不受影响。
        self.order_time = order_time
        # 委托状态描述 —— MiniQMT XtOrder.status_msg 的语义 (如废单原因)。
        # 柜台的拒单理由只在这里, 例如
        # "[COUNTER] 资金可用余额不足，尚需[4789.630]" (issue #60)。
        self.status_msg = status_msg
        # 完整 QMT 的不同版本可能不提供 price_type；追加在末尾保持旧位置参数兼容。
        self.price_type = price_type
        # 下面这五个是 MiniQMT XtOrder 契约里有、本桥一直没发的字段
        # (issue #133)。xttype.XtOrder 的构造参数就列着 secu_account /
        # instrument_name，并在 __init__ 里置 account_type。
        # account_type 是 xtconstant 的数字码（0 = 未知，由客户端兜底）。
        self.account_type = account_type
        self.instrument_name = instrument_name
        self.secu_account = secu_account
        self.offset_flag = offset_flag
        self.direction = direction
        # 官方 Order 字段 m_dTradeAmount(成交金额; 期货 = 均价×数量×合约乘数)。
        # 柜台自己给的成交金额, 不用调用方拿价格乘数量去算 (issue #173)。
        # 追加在末尾并给默认值, 保持既有位置参数调用不受影响。
        # 0.0 = 未成交, 或该终端的 ORDER 行不带这个字段。
        self.trade_amount = trade_amount


class TradeSnapshot:
    def __init__(self, trade_id, order_sys_id, stock_code, action, volume, price,
                 traded_at="", user_order_id="", amount=0.0, strategy_name="",
                 traded_time=0, account_type=0, instrument_name="",
                 secu_account="", commission=0.0, offset_flag=None,
                 direction=None):
        self.trade_id = trade_id
        self.order_sys_id = order_sys_id
        self.stock_code = stock_code
        self.action = action
        self.volume = volume
        self.price = price
        self.traded_at = traded_at
        self.user_order_id = user_order_id
        # 官方 Deal 字段 m_dTradeAmount(成交额) / m_strTradeDate+m_strTradeTime。
        # 追加在末尾并给默认值, 保持既有位置参数调用不受影响 (同 OrderSnapshot.order_time)。
        self.amount = amount
        # strategy_name 优先取 DEAL 行自己的 m_strStrategyName；取不到才回填查询
        # 过滤参数（按策略过滤时返回集必属该策略）。以前只有后一半，
        # 所以不过滤查全部时这个字段恒为空字符串 (issue #133)。
        self.strategy_name = strategy_name
        self.traded_time = traded_time
        # 同 OrderSnapshot：MiniQMT XtTrade 契约里有而本桥没发的字段。
        # commission 是 XtTrade 独有的（手续费）。
        self.account_type = account_type
        self.instrument_name = instrument_name
        self.secu_account = secu_account
        self.commission = commission
        self.offset_flag = offset_flag
        self.direction = direction


class OrderRef:
    def __init__(self, order_sys_id, user_order_id=""):
        self.order_sys_id = order_sys_id
        self.user_order_id = user_order_id


class CancelResult:
    def __init__(self, success, message=""):
        self.success = success
        self.message = message
