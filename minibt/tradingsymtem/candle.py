from ..indicators.core import BtIndicator, IndSeries, IndFrame, KLine

class Candle:
    """蜡烛图指标"""
    def __init__(self, data: KLine | IndFrame | IndSeries = None):
        self.data = data