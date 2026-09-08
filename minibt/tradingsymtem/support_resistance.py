from ..indicators.core import BtIndicator, IndSeries, IndFrame, KLine


class SupportResistance:
    """支撑阻力指标"""
    def __init__(self, data: KLine | IndFrame | IndSeries = None):
        self.data = data
