from ..indicators.core import BtIndicator, IndSeries, IndFrame, KLine


class Volume:
    """成交量指标"""
    def __init__(self, data: KLine | IndFrame | IndSeries = None):
        self.data = data
