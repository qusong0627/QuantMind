from .stop import *
from ..other import Meta, base
__all__ = ["BtStop", "Stop"]


class BtStop(base, metaclass=Meta):
    """
    ## 回测止损策略管理器

    提供多种止损策略的统一接口，用于在回测系统中管理止损逻辑。

    该类集成了多种经典的止损算法，可以根据不同的交易需求选择合适的止损策略。

    ### 可用止损策略:

    - **CAC40**: CAC40指数专用止损策略，采用分段利润跟踪机制
    - **SegmentationTracking**: 分段跟踪止损，基于价格波动幅度动态调整止损位
    - **TimeSegmentationTracking**: 时间分段跟踪止损，结合持仓天数进行止损调整
    - **TrailingStopLoss**: 标准跟踪止损，当价格向有利方向移动时调整止损位
    - **AnotherATRTrailingStop**: 另一种ATR跟踪止损变体
    - **FixedStop**: 固定点数止损，同时设置止损价和目标价
    - **FSStop**: 仅固定点数止损，不设置目标价
    - **FTStop**: 仅固定点数目标价，不设置止损价

    ### 使用示例:
    ```python
    # 创建固定止损策略实例
    self.data.buy(BtStop.FixedStop(stop_distance=10, target_distance=20))

    # 创建分段跟踪止损策略实例
    self.data.buy(stop=BtStop.SegmentationTracking(
        length=14, 
        mult=2.0, 
        method="atr",
        acceleration=[0.382, 0.5, 0.618, 1.0]
    ))
    # 默认参数
    self.data.buy(stop=BtStop.SegmentationTracking)
    ```

    ### 设计特点:
    1. **策略多样性**: 提供从简单固定止损到复杂动态跟踪止损的多种选择
    2. **参数可配置**: 每种策略都提供丰富的参数调节选项
    3. **易于扩展**: 基于元类的设计便于添加新的止损策略
    4. **统一接口**: 所有策略都遵循相同的调用接口，便于在回测系统中切换使用
    """

    # CAC40指数专用止损策略，采用分段利润跟踪机制
    CAC40 = CAC40

    # 分段跟踪止损策略，基于价格波动幅度动态调整止损位
    SegmentationTracking = SegmentationTracking

    # 时间分段跟踪止损策略，结合持仓天数进行止损调整
    TimeSegmentationTracking = TimeSegmentationTracking

    # 标准跟踪止损策略，当价格向有利方向移动时调整止损位
    TrailingStopLoss = TrailingStopLoss

    # 另一种ATR跟踪止损变体策略
    AnotherATRTrailingStop = AnotherATRTrailingStop

    # 固定点数止损策略，同时设置止损价和目标价
    FixedStop = FixedStop

    # 仅固定点数止损策略，不设置目标价
    FSStop = FSStop

    # 仅固定点数目标价策略，不设置止损价
    FTStop = FTStop


# 创建单例实例
BtStop = BtStop()
