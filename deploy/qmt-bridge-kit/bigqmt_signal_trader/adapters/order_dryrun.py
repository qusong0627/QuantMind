"""不发真实委托的下单 gateway，用于联调和回放。"""

import hashlib

from ..models import OrderSubmitResult


class DryRunOrderGateway:
    def __init__(self):
        self.submitted = []
        self.cancelled = []

    def submit(self, request):
        self.submitted.append(request)
        digest = hashlib.sha1(request.signal_id.encode("utf-8")).hexdigest()[:10]
        return OrderSubmitResult(
            status="DRY_RUN",
            user_order_id=f"dryrun:bq:{digest}:{request.signal_id}",
            order_sys_id=None,
        )

    def cancel(self, order_ref, account_id=None):
        self.cancelled.append(order_ref)
        return None

    def query_orders(self, account_id, strategy_name):
        return []

    def query_trades(self, account_id, strategy_name):
        return []
