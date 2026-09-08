from ..indicators.core import BtIndicator, IndSeries, IndFrame, KLine


class Momentum:
    """动量指标"""
    def __init__(self, data: KLine | IndFrame | IndSeries = None):
        self.data = data
