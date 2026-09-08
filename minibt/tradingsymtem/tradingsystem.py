from ..indicators.core import IndSeries, IndFrame, KLine
from .performance import Performance
from .candle import Candle
from .support_resistance import SupportResistance
from .pair import Pair
from .statistics import Statistics
from .cycle import Cycle
from .volume import Volume
from .overlap import Overlap
from .momentum import Momentum
from .trend import Trend
from .volatility import Volatility


class TradingSystem:
    """交易系统基类，包含完整的指标分类体系（按照 pandas_ta 分类结构）"""
    
    def __init__(self, data: KLine | IndFrame | IndSeries = None):
        self.data = data
    
    @property
    def overlap(self) -> Overlap:
        """重叠指标"""
        return Overlap(self.data)
    
    @property
    def momentum(self) -> Momentum:
        """动量指标"""
        return Momentum(self.data)
    
    @property
    def trend(self) -> Trend:
        """趋势指标"""
        return Trend(self.data)
    
    @property
    def volatility(self) -> Volatility:
        """波动率指标"""
        return Volatility(self.data)
    
    @property
    def volume(self) -> Volume:
        """成交量指标"""
        return Volume(self.data)
    
    @property
    def cycle(self) -> Cycle:
        """周期指标"""
        return Cycle(self.data)
    
    @property
    def statistics(self) -> Statistics:
        """统计指标"""
        return Statistics(self.data)
    
    @property
    def performance(self) -> Performance:
        """表现指标"""
        return Performance(self.data)
    
    @property
    def candle(self) -> Candle:
        """蜡烛图指标"""
        return Candle(self.data)
    
    @property
    def support_resistance(self) -> SupportResistance:
        """支撑阻力指标"""
        return SupportResistance(self.data)
    
    @property
    def sr(self) -> SupportResistance:
        """支撑阻力指标（简写）"""
        return self.support_resistance
    
    @property
    def pair(self) -> Pair:
        """配对指标"""
        return Pair(self.data)
        
        
        