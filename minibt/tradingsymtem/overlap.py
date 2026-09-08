from ..indicators.core import BtIndicator, IndSeries, IndFrame, KLine
class Overlap:
    """重叠指标"""
    def __init__(self, data: KLine | IndFrame | IndSeries = None):
        self.data = data
