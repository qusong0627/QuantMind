"""大 QMT 运行环境适配器骨架。"""

import datetime as _dt


class BigQmtRuntimeAdapter:
    def __init__(self, context_info):
        self.context_info = context_info

    def now(self):
        return _dt.datetime.now()

    @staticmethod
    def to_order_event(order):
        return order

    @staticmethod
    def to_trade_event(trade):
        return trade
