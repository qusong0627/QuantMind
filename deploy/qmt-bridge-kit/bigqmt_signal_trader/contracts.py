"""可替换 adapter 的接口定义。"""

import datetime as _dt
from typing import Dict, List

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    try:
        from typing_extensions import Protocol
    except ImportError:
        # Very old embedded environments (QMT built-in Python 3.6 without
        # typing_extensions): Protocol is only used for structural typing
        # documentation here, so a plain stand-in keeps imports working.
        class Protocol(object):
            pass

from .models import (
    AccountSnapshot,
    AssetSnapshot,
    CancelResult,
    OrderRef,
    OrderRequest,
    OrderSnapshot,
    OrderSubmitResult,
    PositionSnapshot,
    TradeSignal,
    TradeSnapshot,
)


class SignalSource(Protocol):
    def fetch(self, account_id: str, limit: int) -> List[TradeSignal]:
        ...

    def ack(self, signal: TradeSignal) -> None:
        ...


class MarketDataProvider(Protocol):
    def get_ticks(self, codes: List[str]) -> Dict[str, dict]:
        ...

    def get_instrument(self, code: str) -> dict:
        ...


class PositionProvider(Protocol):
    def get_positions(self, account_id: str) -> Dict[str, PositionSnapshot]:
        ...

    def get_asset(self, account_id: str) -> AssetSnapshot:
        ...


class OrderGateway(Protocol):
    def submit(self, request: OrderRequest) -> OrderSubmitResult:
        ...

    def cancel(self, order_ref: OrderRef) -> CancelResult:
        ...

    def query_orders(self, account_id: str, strategy_name: str) -> List[OrderSnapshot]:
        ...

    def query_trades(self, account_id: str, strategy_name: str) -> List[TradeSnapshot]:
        ...


class PositionSyncSink(Protocol):
    def publish(self, snapshot: AccountSnapshot) -> None:
        ...


class StateStore(Protocol):
    def claim(self, signal: TradeSignal, consumer_id: str) -> bool:
        ...

    def mark_submitted(self, signal_id: str, result: OrderSubmitResult) -> None:
        ...

    def mark_finished(self, signal_id: str, status: str, message: str = "") -> None:
        ...


class RuntimeAdapter(Protocol):
    def now(self) -> _dt.datetime:
        ...
