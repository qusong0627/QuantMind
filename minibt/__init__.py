# -*- coding: utf-8 -*-
"""
minibt: 一站式量化交易策略开发库
=====================================
- minibt 是一个专注于简化量化交易全流程的开发库，支持从策略编写、指标计算、
- 回测分析到实盘对接的完整链路，提供极简的API设计和丰富的工具链，助力快速落地量化想法。

核心功能:
- 内置丰富金融指标（TA-Lib、Pandas TA等），支持自定义指标扩展
- 高效回测引擎，支持多维度性能分析与参数优化
- 无缝对接实盘接口（如TQSDK），策略一键切换回测/实盘模式
- 集成可视化工具（Bokeh）与UI界面（PyQt），简化策略调试与分析

相关资源:
- 作者: owen
- 版本信息: v1.2.7
- 许可证: MIT License
- GitHub仓库: https://github.com/MiniBtMaster/minibt
- PyPI仓库: https://pypi.org/project/minibt/
- 项目教程：https://www.minibt.cn
- 联系邮箱：407841129@qq.com
"""
from .logger import LogLevel
# [pilot-prune] from .elegantrl.agents import Agents, BestAgents
from .stop import BtStop
from .bt import Bt
from .callback import stop_callback
from .strategy.strategy import Strategy
from .strategy.base import SizeType, CommissionType
from .constant import *
from .utils import (
    Config,
    FILED,
    OptunaConfig,
    tq_auth,
    tq_account,
    Multiply,
    CandleStyle,
    LineStyle,
    SignalStyle,
    SpanStyle,
    Markers,
    WaterMark,
    PlotInfo,
    MaType,
    SignalLabel,
    OrderType,
    options,
    np,
    Gui,
    OpConfig,
    TradeTimeFilter,
)
from .data.utils import LocalDatas  # 本地数据源管理
from .indicators import (
    TradingView,  # 从 indicators 子包导入
    # 指标构造器
    BtIndicator,
    # 内置指标类型
    KLine,
    IndSeries,
    IndFrame,
    Line,
    SeriesType,
    # 第三方指标库封装
    PandasTa,
    TaLib,
    BtInd,
    TqTa,
    TqFunc,
    TuLip,
    FinTa,
    Pair,
    Factors,
    Stop,
    # 指标基类与核心指标
    IndicatorClass,
    CoreIndicators,
    OptimizeConfig,
    pd,

)

__author__ = "owen"
__copyright__ = "Copyright (c) 2025 minibt开发团队"
__license__ = "MIT"
__version__ = "1.2.7"
__version_info__ = (1, 2, 7)
__description__ = "一站式量化交易策略开发库，简化从策略搭建到实盘交易的全流程"
__url__ = "https://www.minibt.cn"
__email__ = "407841129@qq.com"



# ------------------------------
# 公共接口导出（__all__定义）
# 仅暴露需要用户直接使用的类/函数/常量，隐藏内部实现
# ------------------------------
__all__ = [
    # 核心框架
    'Bt', 'Strategy',
    # 配置
    'Config', 'OptunaConfig', 'FILED',
    # 数据
    'LocalDatas', 'KLine',
    # 指标，只保留常用指标
    'BtIndicator', 'PandasTa', 'TaLib', 'BtInd', 'TqTa',
    'TqFunc', 'TuLip', 'FinTa', 'Pair', 'Factors', 'TradingView',
    'BtStop', 'Stop', 'IndicatorClass', 'CoreIndicators',
    # 工具
    'np', 'pd', 'tq_auth', 'tq_account', 'Multiply',
    # 设置
    'options',
    # 订单类型
    'OrderType',
    # 可视化样式
    'LineDash', 'Colors', 'Markers', 'WaterMark','Line','SeriesType',
    'CandleStyle', 'LineStyle', 'SignalStyle', 'SpanStyle', 'PlotInfo', 'MaType', "SignalLabel",
    # 强化学习
    'Agents', 'BestAgents',
    # 类型
    'IndSeries', 'IndFrame', 'IndSeries', 'IndFrame',
    # 日志相关
    'LogLevel',
    # 策略相关
    'SizeType', 'CommissionType',
    # 回调装饰器
    'stop_callback',
    # 优化相关
    'OptimizeConfig',
    # 图形界面
    'Gui',
    'OpConfig',
    # 交易时间过滤器
    'TradeTimeFilter',
]


# 初始化本地数据源
LocalDatas.rewrite(True)
