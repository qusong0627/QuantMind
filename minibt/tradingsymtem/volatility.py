from ..indicators.core import BtIndicator, IndSeries, IndFrame, KLine


class Volatility:
    """波动率指标"""
    def __init__(self, data: KLine | IndFrame | IndSeries = None):
        self.data = data
