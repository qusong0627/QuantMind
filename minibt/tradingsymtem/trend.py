from ..indicators.core import BtIndicator, IndSeries, IndFrame, KLine


class Trend:
    """趋势指标"""
    def __init__(self, data: KLine | IndFrame | IndSeries = None):
        self.data = data
