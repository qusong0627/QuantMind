"""信号交易应用编排层。"""

import datetime as _dt

from .models import AccountSnapshot, OrderRequest, SignalAction
from .price_engine import build_order_price
from .risk_guard import validate_signal


class SignalTradingApp:
    def __init__(
        self,
        account_id,
        signal_source,
        market_data,
        position_provider,
        order_gateway,
        position_sync_sink,
        state_store,
        consumer_id="bigqmt-signal-trader",
        fetch_limit=20,
    ):
        self.account_id = account_id
        self.signal_source = signal_source
        self.market_data = market_data
        self.position_provider = position_provider
        self.order_gateway = order_gateway
        self.position_sync_sink = position_sync_sink
        self.state_store = state_store
        self.consumer_id = consumer_id
        self.fetch_limit = int(fetch_limit)

    def tick(self, now=None):
        now = now or _dt.datetime.now()
        signals = self.signal_source.fetch(self.account_id, self.fetch_limit)
        positions = self.position_provider.get_positions(self.account_id)

        for signal in signals:
            if not self.state_store.claim(signal, self.consumer_id):
                continue
            try:
                self._handle_signal(signal, now, positions)
            except Exception as exc:
                self.state_store.mark_finished(signal.signal_id, "FAILED", str(exc))
                self.signal_source.ack(signal)

        self.sync_positions("tick", now=now)

    def _handle_signal(self, signal, now, positions):
        decision = validate_signal(signal, now, positions)
        if not decision.allowed:
            self.state_store.mark_finished(signal.signal_id, "SKIPPED", decision.reason)
            self.signal_source.ack(signal)
            return

        price = build_order_price(
            self.market_data,
            decision.stock_code,
            signal.action.value,
            price_type=signal.price_type,
            fixed_price=signal.price,
        )
        request = OrderRequest(
            signal_id=signal.signal_id,
            account_id=signal.account_id,
            action=signal.action.value,
            stock_code=decision.stock_code,
            volume=decision.volume,
            price=price,
            price_type="LIMIT",
            strategy_name=signal.strategy_name,
            remark=signal.remark,
        )
        result = self.order_gateway.submit(request)
        self.state_store.mark_submitted(signal.signal_id, result)
        self.signal_source.ack(signal)

    def on_init(self, runtime):
        return None

    def on_order_event(self, event):
        return None

    def on_trade_event(self, event):
        self.sync_positions("trade_event")

    def sync_positions(self, reason, now=None):
        now = now or _dt.datetime.now()
        asset = self.position_provider.get_asset(self.account_id)
        positions = self.position_provider.get_positions(self.account_id)
        snapshot = AccountSnapshot(
            account_id=self.account_id,
            asset=asset,
            positions=positions,
            reason=reason,
            updated_at=now,
        )
        self.position_sync_sink.publish(snapshot)
