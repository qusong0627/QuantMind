"""可替换的大 QMT 信号下单包核心模块。"""

from .version import __version__, deployment_report

from .app import SignalTradingApp
from .models import (
    AccountSnapshot,
    AssetSnapshot,
    OrderRequest,
    OrderSubmitResult,
    PositionSnapshot,
    SignalAction,
    SignalStatus,
    TradeSignal,
)
from .option_analytics import (
    black_scholes_price,
    calculate_option_analytics,
    implied_volatility,
    option_greeks,
)
from .xtquant_compat import BigQmtRpcClient, BigQmtXtData, BigQmtXtTrader

__all__ = [
    "AccountSnapshot",
    "AssetSnapshot",
    "BigQmtRpcClient",
    "BigQmtXtData",
    "BigQmtXtTrader",
    "OrderRequest",
    "OrderSubmitResult",
    "PositionSnapshot",
    "SignalAction",
    "SignalStatus",
    "SignalTradingApp",
    "TradeSignal",
    "__version__",
    "black_scholes_price",
    "calculate_option_analytics",
    "implied_volatility",
    "option_greeks",
]
