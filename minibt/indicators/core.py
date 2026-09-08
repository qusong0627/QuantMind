from __future__ import annotations

import os
from dataclasses import dataclass

from networkx import dfs_successors
from .factors import Factors
from .pair import Pair
from .finta import FinTa
from .tqta import TqTa
from .tqfunc import TqFunc
from .tulip import TuLip
from .btind import BtInd
from .talib import TaLib
from .pandas_ta import PandasTa
from .base import IndicatorsBase, KLineSetting
from ..utils import \
    (TYPE_CHECKING, PlotInfo, np, pd, Callable, BtID,
     partial, Iterable, Lines,  Meta, LineStyle, IndSetting, SymbolInfo, Broker,
     DataFrameSet, Addict, common, LineDash, Colors, field,
     Multiply, ispandasojb, Literal, LineStyleType,
     Category, SIGNAL_Str, set_property, Cache, FILED, SignalStyleType,
     default_symbol_info, CandlesCategory, Quotes, Quote, BtAccount, TqAccount, Position, warnings,
     BtPosition, getsourcelines, TqObjs, SpanStyle, default_signal_style,
     time_to_datetime, CandleStyle, SignalStyle, LineAttrType,
     SignalAttrType, CategoryString, SpanList, TradeTimeFilter,
     PandasDataFrame, PandasSeries,  SignalLabel,  Orders, Markers,
     options, SPECIAL_FUNC, datetime, OrderType, Order, WaterMark, DataSetBase)
from ..other import ProcessedAttribute
from pandas.core.indexing import _iLocIndexer, _LocIndexer
from .base import tobtind
from ..order import OrderSide


if TYPE_CHECKING:
    from typing_ import *
    from .tradingview import TradingView
    from ..strategy.strategy import Strategy
    from ..bt import Bt
    from ..utils import CoreFunc, corefunc, Params
    from ..other import SizeType, CommissionType
    from tqsdk.objs import Order as TqOrder


# 前向引用（避免循环导入）
class IndFrame:
    pass


class IndSeries:
    pass


class Line:
    pass


class KLine:
    pass


class SignalBacktestResult:
    pass


# 保存原始函数
original_apply_if_callable = common.apply_if_callable


def apply_if_callable_(maybe_callable, obj, **kwargs):
    """自定义的apply_if_callable函数"""
    if callable(maybe_callable):
        # 检查是否是 BtIndType 实例或其类型在 BtIndType 中
        if isinstance(maybe_callable, BtIndType) or type(maybe_callable) in BtIndType:
            return maybe_callable
        return maybe_callable(obj, ** kwargs)
    return maybe_callable


# 替换pandas模块中的函数
common.apply_if_callable = apply_if_callable_

# ======================== 索引器类 ========================


class MinibtIndexerBase:
    """
    minibt索引器基类 - 为所有索引器提供通用功能
    """

    def __init__(self, name: str, obj):
        self.name = name
        self.obj = obj

    def _convert_to_minibt(self, data):
        """
        智能转换为minibt数据结构
        """
        # iloc/loc 索引器总是需要进行类型转换以保持一致性
        if ispandasojb(data):
            return self._create_minibt_object(data)
        return data

    def _create_minibt_object(self, pandas_obj):
        """
        根据pandas对象创建对应的minibt对象
        """
        try:
            indicator_kwargs = self.obj.get_indicator_kwargs()

            # 根据维度创建对应的minibt对象
            if pandas_obj.ndim == 1:
                # 一维数据 -> IndSeries
                return IndSeries(pandas_obj.values, **indicator_kwargs)
            elif pandas_obj.ndim == 2:
                # 二维数据
                # 检查是否为KLine数据
                if self._is_kline_data(pandas_obj):
                    return self._create_kline(pandas_obj, **indicator_kwargs)
                else:
                    # 普通二维数据 -> IndFrame
                    indicator_kwargs["lines"] = list(pandas_obj.columns)
                    return IndFrame(pandas_obj, **indicator_kwargs)

            # 默认返回原对象
            return pandas_obj

        except Exception as e:
            # 其他错误，记录并返回原始对象
            warnings.warn(f"转换内置指标失败: {e}")
            return pandas_obj

    def _is_kline_data(self, pandas_obj):
        """
        检查是否为K线数据
        """
        try:
            return set(FILED.ALL) == set(pandas_obj.columns)
        except:
            return False

    def _create_kline(self, pandas_obj, **kwargs):
        """
        创建KLine对象
        """
        data = pandas_obj.copy()
        try:
            if hasattr(self.obj, "_klinesetting"):
                symbol_info = self.obj._klinesetting.symbol_info
            else:
                symbol_info = default_symbol_info(data)
            data.add_info(**symbol_info)
            data = KLine(data, **kwargs)
        except Exception as e:
            pass
        return data

    def _after_setitem(self, data=None) -> None:
        """
        设置项目后的统一处理
        """
        # 更新线数据
        self.obj._update_line_data(data)

        # 更新上采样数据
        if self.obj.strategy_instances and self.obj._dataset.upsample_object is not None:
            setattr(self.obj.strategy_instance, self.obj._upsample_name,
                    self.obj.upsample(reset=True))


class MinibtILocIndexer(MinibtIndexerBase, _iLocIndexer):
    """
    基于整数位置的索引器 - 继承自pandas的_iLocIndexer
    """

    def __init__(self, name: str, obj) -> None:
        # 分别调用两个父类的__init__
        MinibtIndexerBase.__init__(self, name, obj)
        _iLocIndexer.__init__(self, name, obj)

    def __getitem__(self, key):
        """
        获取项目 - 自动转换为minibt对象
        """
        # 调用pandas的__getitem__获取数据
        data = _iLocIndexer.__getitem__(self, key)
        # 转换为minibt对象
        return self._convert_to_minibt(data)

    def __setitem__(self, key, value) -> None:
        """
        设置项目 - 自动处理minibt特定逻辑
        """
        # 调用pandas的__setitem__
        _iLocIndexer.__setitem__(self, key, value)
        self.obj.pandas_object.iloc[key] = value

        # 执行minibt特定的后处理
        self._after_setitem()


class MinibtLocIndexer(MinibtIndexerBase, _LocIndexer):
    """
    基于标签的索引器 - 继承自pandas的_LocIndexer
    """

    def __init__(self, name: str, obj):
        # 分别调用两个父类的__init__
        MinibtIndexerBase.__init__(self, name, obj)
        _LocIndexer.__init__(self, name, obj)

    def __getitem__(self, key):
        """
        获取项目 - 自动转换为minibt对象
        """
        # 调用pandas的__getitem__获取数据
        data = _LocIndexer.__getitem__(self, key)
        # 转换为minibt对象
        return self._convert_to_minibt(data)

    def __setitem__(self, key, value) -> None:
        """
        设置项目 - 自动处理minibt特定逻辑
        """
        # 调用pandas的__setitem__
        _LocIndexer.__setitem__(self, key, value)
        self.obj.pandas_object.loc[key] = value

        # 执行minibt特定的后处理
        self._after_setitem()


class SeriesType:
    """IndFrame指标线Line转IndSeries"""

    def __init__(self, indframe):
        self.indframe = indframe

    def __getattr__(self, key) -> 'IndSeries':
        from ..utils import Lines, DefaultIndicatorConfig
        id = self.indframe.id.copy()
        sname = key
        ind_name = key
        lines = Lines(key)
        isplot = self.indframe.isplot
        if isinstance(isplot, dict):
            isplot = isplot.get(key, True)
        overlap = self.indframe.overlap
        if isinstance(overlap, dict):
            overlap = overlap.get(key, False)
        config = DefaultIndicatorConfig(
            id,
            sname,
            ind_name,
            lines,
            None,
            isplot,
            self.indframe.ismain,
            self.indframe.isreplay,
            self.indframe.isresample,
            overlap,
            True,
            self.indframe.iscustom,
        )
        return IndSeries(getattr(self.indframe, key).values, **config.to_dict())


class CoreIndicators:
    """
    ## 核心指标计算类

    该类作为技术指标计算的统一入口，提供对多种技术分析库的便捷访问。
    通过不同的方法前缀，可以调用来自不同技术分析库的指标计算方法。

    ## 支持的指标库前缀：
    - `pta_` : PandasTA 指标 - 基于pandas的高性能技术分析库
    - `btind_` : BtInd 指标 - 回测专用指标库
    - `ti_` : TuLip 指标 - 传统技术分析指标库
    - `talib_` : TA-Lib 指标 - 行业标准技术分析库（C语言实现，速度快）
    - `finta_` : FinTA 指标 - 金融技术分析库
    - `tqfunc_` : TqFunc 指标 - 天勤量化函数库
    - `tqta_` : TqTa 指标 - 天勤技术分析库
    - `pair_` : Pair 指标 - 配对交易专用指标
    - `factor_` : Factors 指标 - 多因子模型指标

    ## 核心特点：
    1. **统一接口**：通过单一对象访问多个技术分析库
    2. **数据一致性**：自动处理数据格式转换和预处理
    3. **性能优化**：底层使用高效计算库（如TA-Lib）
    4. **类型安全**：返回标准的IndSeries/IndFrame对象

    ## 使用方式：
    方式1：通过CoreIndicators类直接访问
    ```python
    ci = CoreIndicators(data)  # data可以是IndSeries/IndFrame/pd.Series/pd.DataFrame
    indicators = ci.indicators  # 获取指标计算器
    sma = indicators.pta_sma(20)  # 计算20日简单移动平均
    rsi = indicators.talib_rsi(14)  # 计算14日RSI
    ```

    方式2：通过内置属性访问（推荐）
    ```python
    # 假设kline是一个包含价格数据的IndFrame
    ma = kline.close.core_indicators.pta_sma(30)  # 直接访问
    ```
    """

    _df: IndFrame | IndSeries

    def __init__(self, data):
        self._df = data

    @property
    def indicators(self) -> CoreFunc:
        """
        ## 指标计算器访问属性

        返回CoreFunc对象，提供对所有技术指标计算方法的访问。

        Returns
        -------
        CoreFunc
            核心函数计算器，包含所有前缀的指标方法

        Examples
        --------
        ```python
        # 获取指标计算器
        ci = CoreIndicators(close_prices)
        calc = ci.indicators

        # 计算不同库的指标
        # PandasTA库的SMA
        sma_pd = calc.pta_sma(20)

        # TA-Lib库的RSI
        rsi_talib = calc.talib_rsi(14)

        # TuLip库的布林带
        bb_ti = calc.ti_bbands(20, 2)

        # 多因子模型的合并因子
        merged_factor = calc.factor_optimizer(factor1, factor2, factor3)
        ```
        """
        return self._df.ta


def _show_equity_chart(sbr):
    """用 matplotlib 绘制权益曲线（CLI 环境最可靠的图表方案）。"""
    import matplotlib
    try:
        matplotlib.use('TkAgg')
    except Exception:
        pass  # 如果 pyplot 已导入，use() 会报错，用已有 backend
    import matplotlib.pyplot as plt
    try:
        eq = sbr._equity_curve
        if eq is None or len(eq) == 0:
            print("[plot] 无权益曲线数据")
            return
        print("[plot] 正在显示权益曲线（关闭图表窗口继续运行）...")
        plt.figure(figsize=(12, 5))
        plt.plot(eq.index if hasattr(eq, 'index') else range(len(eq)),
                 eq.values if hasattr(eq, 'values') else eq,
                 color='#1f77b4', linewidth=1)
        plt.title(f'Equity Curve — {sbr.__class__.__name__}')
        plt.xlabel('Bar Index')
        plt.ylabel('Equity')
        plt.grid(True, alpha=0.3)
        plt.tight_layout()
        plt.show()  # blocking — 用户关闭窗口后继续运行
    except Exception as e:
        print(f"[plot] matplotlib 显示失败: {e}")


class IndFrame(IndicatorsBase, PandasDataFrame, PandasTa, TaLib):
    """
    # 框架内置指标数据容器类（IndFrame 类型）
    - 核心定位：继承 pandas.DataFrame 并整合指标计算、可视化配置、交易信号管理能力，作为系统统一的多列指标数据格式

    ### 📘 **文档参考**:
    - 类简介：https://www.minibt.cn/minibt_basic/1.10minibt_internal_data_dataframe_guide/

    ### 核心特性：
    1. 多父类融合：
    - 继承 `pd.DataFrame`：保留原生 DataFrame 的数据存储与计算能力（如索引、切片、列操作）
    - 继承 `IndicatorsBase`：获得指标基础属性与方法（如指标ID、分类标识）
    - 继承 `PandasTa`/`TaLib`：直接调用 pandas_ta、TaLib 库的技术指标计算接口
    2. 指标化增强：
    - 将每列数据自动封装为 `Line` 类型（框架内置单指标序列），支持指标属性（如绘图样式、信号标记）
    - 内置指标元数据管理（`_plotinfo` 绘图配置、`_indsetting` 指标设置），无需额外定义
    3. 交易信号原生支持：
    - 内置多头/空头开仓/离场信号（`long_signal`/`short_signal`/`exitlong_signal`/`exitshort_signal`）
    - 支持信号样式自定义（颜色、标记、大小等），直接关联绘图逻辑
    4. 可视化配置集成：
    - 支持线型（`line_style`）、信号样式（`signal_style`）的批量/单独设置
    - 自动适配框架绘图模块，无需手动传递绘图参数
    5. 数据兼容性：
    - 支持多种输入数据类型（`pd.DataFrame`/`np.ndarray`/列表/元组/字典），自动转换为标准格式
    - 提供 `to_lines()`/`to_ndarray()` 方法，便捷转换为 `Line` 序列或 numpy 数组

    ### 初始化参数说明：
    Args:
        data: 输入数据，支持以下类型：
            - pd.DataFrame：直接使用已有 DataFrame，列名自动作为指标线名称
            - np.ndarray：numpy 数组，需通过 `kwargs` 指定 `lines`（指标线名称）
            - tuple/list：整数序列（如 (100, 3) 表示 100 行 3 列），自动生成全 NaN 数组（标记为自定义数据）
            - dict：键为列名、值为数据的字典，自动转换为 DataFrame
        **kwargs: 额外配置参数（核心参数如下）：
            - lines (list[str]): 指标线名称列表，未指定时自动读取 DataFrame 列名
            - id (BtID): 指标唯一标识（默认自动生成），用于指标区分与管理
            - isplot (bool/dict): 是否绘图（True/False 或按列名配置的字典），默认 True
            - category (str): 指标分类（如 "candles"/"momentum"，默认 Category.Any）
            - overlap (bool): 是否与主图重叠显示（默认 False）
            - isha/islr (bool): 是否为 Heikin-Ashi 蜡烛图/线性回归指标（默认 False）
            - ismain (bool): 是否为主图指标（默认 False）
            - isindicator (bool): 是否为技术指标（默认 True，非指标数据设为 False）
            - iscustom (bool): 是否为自定义数据（默认 False，列表/元组输入时自动设为 True）
            - height (int): 指标绘图高度（蜡烛图默认 300，其他默认 150）

    ### 核心属性说明：
    1. 数据与指标基础属性：
    - IndSeries: 转换为框架内置 `SeriesType`（单列指标容器）
    - _plotinfo: `PlotInfo` 实例，存储绘图配置（如高度、线型、信号样式、指标分类）
    - _indsetting: `IndSetting` 实例，存储指标元数据（如指标ID、维度、自定义标识）
    - _dataset: `DataFrameSet` 实例，管理数据副本与缓存
    2. 交易信号属性（支持赋值与读取）：
    - long_signal: 多头开仓信号（需为与数据长度一致的可迭代对象）
    - exitlong_signal: 多头离场信号
    - short_signal: 空头开仓信号
    - exitshort_signal: 空头离场信号
    3. 样式配置属性（支持批量/单独设置）：
    - signal_style: 信号样式（标记、颜色、大小等），返回 `SignalStyleType` 实例
    - line_style: 指标线型（线型、线宽、颜色等），返回 `LineStyleType` 实例
    - signal_color/line_color: 单独设置信号/线型颜色，返回 `SignalAttrType`/`LineAttrType` 实例
    - 其他样式属性：signal_key（信号位置）、signal_size（信号大小）、line_dash（线型虚实）等

    ### 使用示例：
    >>> # 1. 从 DataFrame 初始化指标 IndFrame
    >>> raw_df = pd.DataFrame({"close": [100, 101, 102, 103], "ma5": [99, 100, 101, 102]})
    >>> ind_df = IndFrame(raw_df, category="overlap", isplot=True)
    >>>
    >>> # 2. 设置多头信号
    >>> ind_df.long_signal = [0, 1, 0, 1]  # 第2、4期为多头开仓信号
    >>>
    >>> # 3. 自定义信号样式
    >>> ind_df.signal_style.long_signal = SignalStyle("low", Colors.blue, Markers.circle_dot, size=25)
    >>>
    >>> # 4. 转换为 Line 序列
    >>> close_line, ma5_line = ind_df.to_lines("close", "ma5")
    >>>
    >>> # 5. 调用 pandas_ta 指标（继承自 PandasTa）
    >>> rsi_df = ind_df.rsi(length=14)
    """
    stop_price: Line
    target_price: Line

    def __init__(self, data: pd.DataFrame | np.ndarray | tuple[int] | list[int] | dict | None = None, **kwargs) -> None:
        if data is None:  # 空数据 → 创建空指标
            data = pd.DataFrame(dtype=np.float64)
            kwargs.setdefault('lines', [])
            kwargs.setdefault('iscustom', True)
        if isinstance(data, dict):  # 字典
            data = pd.DataFrame(data)
        if isinstance(data, (tuple, list)):  # 自定义数据
            assert len(data) > 1, "维度shape的长度必须大于1"
            assert all([isinstance(num, int) and num > 0
                       for num in data]), "传入数据为数组时元素必须为大于0的整数"
            data = np.full(data, np.nan)
            kwargs.update(dict(iscustom=True))
        if 'lines' not in kwargs:
            # 没定义lines时采用pd.DataFrame的列名
            if isinstance(data, pd.DataFrame):
                kwargs['lines'] = list(data.columns)
        if hasattr(data, "pandas_object"):
            data = data.pandas_object
        super().__init__(data, columns=kwargs.pop("lines"))
        # assert lines in kwargs
        btid = kwargs.pop("id", BtID())
        if isinstance(btid, dict):
            btid = BtID(**btid)
        if not isinstance(btid, BtID):
            btid = BtID()
        lines: list[str] = list(self.columns)
        lines = Lines(*lines)(self)
        isplot: bool | dict = kwargs.pop('isplot', True)
        if not isinstance(isplot, (bool, dict)):
            isplot = True
        category = kwargs.pop('category', Category.Any)
        if not isinstance(category, str) or not category:
            category = Category.Any
        if not isinstance(category, CategoryString):
            category = CategoryString(category)
        overlap = kwargs.pop('overlap', False)
        isha: bool = kwargs.pop('isha', False)
        islr: bool = kwargs.pop('islr', False)
        iscandles = "candles" in category
        sname = kwargs.pop('sname', 'name')
        ind_name = kwargs.pop("ind_name", sname)
        candlestyle = iscandles and CandleStyle() or None
        is_mir = kwargs.pop('_is_mir', False)
        ismain = bool(kwargs.pop('ismain', False))
        isreplay = bool(kwargs.pop('isreplay', False))
        isresample = bool(kwargs.pop('isresample', False))
        isindicator = True
        iscustom = bool(kwargs.pop('iscustom', False))
        height = kwargs.pop("height", iscandles and 300 or 150)
        linestyle = kwargs.pop("linestyle", {})
        signalstyle = Addict(kwargs.pop("signalstyle", {}))
        isMDim = True
        dim_match = kwargs.pop("dim_match", True)
        span = kwargs.pop("spanstyle", np.nan)
        # 指标是否有交易信号
        signallines = [string for string in SIGNAL_Str if string in lines]
        # 将IndFrame每列设置为Line
        line_filed: list[str] = list(
            map(lambda x: f"_{x}", lines))
        self._plotinfo = PlotInfo(
            height=height,
            sname=sname,
            ind_name=ind_name,
            lines=lines,
            line_filed=line_filed,
            signallines=signallines,
            category=category,
            isplot=isplot,
            overlap=overlap,
            candlestyle=candlestyle,
            linestyle=linestyle,
            signalstyle=signalstyle,
            spanstyle=span,
        )

        self._indsetting = IndSetting(
            btid,
            is_mir,
            isha,
            islr,
            ismain,
            isreplay,
            isresample,
            isindicator,
            iscustom,
            isMDim,
            dim_match,
        )
        # 名称前加下划线,定义每列数据为Line数据
        for i, line in enumerate(line_filed):
            _isplot = self._plotinfo.isplot
            if not isinstance(_isplot, bool):
                _isplot = _isplot[lines[i]]
            _overlap = self._plotinfo.overlap
            if not isinstance(_overlap, bool):
                _overlap = _overlap[lines[i]]
            # 直接用列索引从底层numpy数组取值,
            # 避免 self[lines[i]] / pd.DataFrame.__getitem__ 
            # 经过 IndicatorsBase.__getitem__ 包装成新 IndFrame 导致无限递归
            _raw_values = self.values[:, i]
            setattr(self, line, Line(
                self, _raw_values, iscustom=iscustom, id=btid.copy(), sname=lines[i],
                    ind_name=ind_name, lines=[
                        lines[i],], category=Category.Any,
                    isplot=_isplot, ismain=ismain, isreplay=isreplay,
                    isresample=isresample, overlap=_overlap))
        # 邦定property函数,IndFrame每列返回的是Line指标数据
        [set_property(self.__class__, attr) for attr in lines]
        kline = kwargs.pop("kline", None)
        if kline is not None and hasattr(kline, "_indsetting"):
            self._indsetting.id = kline._indsetting.id.copy()
        self._dataset = DataFrameSet(
            pandas_object=self.copy(),
            kline_object=kline,
            source_object=kwargs.pop("source", None),
            copy_object=self.copy())
        if self._indsetting.iscustom:
            self._dataset.custom_object = self.values
        self.cache = Cache(maxsize=np.inf)

    @property
    def series(self) -> SeriesType:
        """### Line转IndSeries"""
        return SeriesType(self)

    @property
    def _Line(self) -> Line:
        return Line

    @property
    def long_signal(self) -> Line:
        """## 多头交易信号"""
        return getattr(self, "_long_signal")

    @long_signal.setter
    def long_signal(self, value) -> None:
        if isinstance(value, Iterable) and len(value) == self.V:
            self["long_signal"] = value

    @property
    def exitlong_signal(self) -> Line:
        """## 多头离场交易信号"""
        return getattr(self, "_exitlong_signal")

    @exitlong_signal.setter
    def exitlong_signal(self, value) -> None:
        if isinstance(value, Iterable) and len(value) == self.V:
            self["exitlong_signal"] = value

    @property
    def short_signal(self) -> Line:
        """## 空头交易信号"""
        return getattr(self, "_short_signal")

    @short_signal.setter
    def short_signal(self, value) -> None:
        if isinstance(value, Iterable) and len(value) == self.V:
            self["short_signal"] = value

    @property
    def exitshort_signal(self) -> Line:
        """## 空头离场交易信号"""
        return getattr(self, "_exitshort_signal")

    @exitshort_signal.setter
    def exitshort_signal(self, value) -> None:
        if isinstance(value, Iterable) and len(value) == self.V:
            self["exitshort_signal"] = value

    @property
    def signal_style(self) -> SignalStyleType:
        """## 信号指标线型设置

        - 提供对信号指标的视觉样式进行精细控制的接口。
        - 可以设置信号线的显示位置、颜色、标记符号、大小等视觉属性。

        Returns:
        >>> SignalStyleType
            SignalStyle配置对象，用于设置信号指标的显示样式

        ### Notes:
        1. 信号线用于在图表上标记交易信号（如买入、卖出点）
        2. 可以分别为不同的信号类型（long_signal, short_signal等）设置不同的样式
        3. SignalStyle包含多个属性：
            - key: 信号线绑定的价格位置（如'low', 'high', 'close'）
            - color: 信号标记的颜色
            - marker: 信号标记的符号
            - overlap: 信号标记是否主图显示
            - show: 信号标记是否显示
            - size: 信号标记的大小
            - label: 信号标签

        Examples:
        ```python
        # 设置买入信号的样式：在最低价位置显示蓝色圆形标记
        self.test.signal_style.long_signal = SignalStyle(
            "low", Colors.blue, Markers.circle_dot, size=25)

        # 设置卖出信号的样式：在最高价位置显示红色三角形标记
        self.test.signal_style.short_signal = SignalStyle(
            "high", Colors.red, Markers.triangle_down, size=25)

        # 查看当前信号样式
        current_style = self.test.signal_style.long_signal
        print(f"买入信号样式: {current_style}")
        ```

        ## Setter: 将所有指标线设置统一SignalStyle

        ```python
        # 将所有信号线统一设置为相同样式
        self.test.signal_style = SignalStyle(
            "low", Colors.blue, Markers.circle_dot, size=25)

        # 这相当于同时设置所有信号类型（long_signal, short_signal等）
        ```
        """
        return SignalStyleType(self)

    @signal_style.setter
    def signal_style(self, value):
        """## 设置所有信号线的统一样式"""
        self._plotinfo.signal_style = value

    @property
    def signal_key(self) -> SignalAttrType:
        """## 信号指标属性key设置

        - 设置信号线绑定的价格位置（如开盘价、最高价、最低价、收盘价）。
        - 这决定了信号标记在K线图上的垂直位置。

        Returns:
        >>> SignalAttrType
            信号属性配置对象，用于设置信号线绑定的价格位置

        ### Notes
        1. key值必须是数据列名，通常是'open', 'high', 'low', 'close'之一
        2. 不同信号类型可以绑定到不同的价格位置
        3. 常用组合：
            - 买入信号：绑定到'low'（在最低价位置显示）
            - 卖出信号：绑定到'high'（在最高价位置显示）

        Examples:
        ```python
        # 设置买入信号绑定到最低价位置
        self.test.signal_key.long_signal = "low"

        # 设置卖出信号绑定到最高价位置
        self.test.signal_key.short_signal = "high"

        # 设置平仓信号绑定到收盘价位置
        self.test.signal_key.exitlong_signal = "close"

        # 查看当前设置
        print(f"买入信号位置: {self.test.signal_key.long_signal}")
        ```

        ## Setter: 将所有指标线LineStyle属性line_color统一设置
        ```python
        # 注意：这里示例描述的是line_color，但实际是signal_key
        # 设置所有信号线都绑定到最低价位置
        self.test.signal_key = "low"

        # 设置所有信号线都绑定到收盘价位置
        self.test.signal_key = "close"
        ```
        """
        return SignalAttrType(self, "key")

    @signal_key.setter
    def signal_key(self, value):
        """## 设置所有信号线的统一价格位置"""
        self._plotinfo.signal_key = value

    @property
    def signal_show(self) -> SignalAttrType:
        """## 信号显示开关设置

        - 控制是否在图表上显示特定的信号线。
        - 可以关闭对应信号显示。

        Returns:
        >>> SignalAttrType
            信号显示控制对象，用于设置信号线的可见性

        ### Notes
        1. 值为True时显示信号，False时隐藏信号
        2. 可以分别控制不同类型信号的显示状态
        3. 在回测可视化时，合理控制信号显示可以避免图表过于杂乱

        Examples:
        ```python
        # 显示买入信号，隐藏卖出信号
        self.test.signal_show.long_signal = True
        self.test.signal_show.short_signal = False

        # 临时隐藏所有信号（调试时使用）
        self.test.signal_show.long_signal = False
        self.test.signal_show.short_signal = False
        self.test.signal_show.exitllong_signal = False

        # 恢复显示所有信号
        for signal_type in ['long_signal', 'short_signal', 'exitllong_signal']:
            setattr(self.test.signal_show, signal_type, True)
        ```

        ## Setter: 统一设置所有信号的显示状态
        ```python
        # 显示所有信号
        self.test.signal_show = True

        # 隐藏所有信号
        self.test.signal_show = False

        # 注意：这会覆盖之前对单个信号的设置
        ```
        """
        return SignalAttrType(self, "show")

    @signal_show.setter
    def signal_show(self, value):
        """设置所有信号线的统一显示状态"""
        self._plotinfo.signal_show = value

    @property
    def signal_color(self) -> SignalAttrType:
        """## 信号颜色设置

        - 设置信号标记的颜色，用于区分不同类型的交易信号。
        - 通常使用红色表示卖出信号，绿色表示买入信号。

        Returns:
        >>> SignalAttrType
            信号颜色配置对象，用于设置信号标记的颜色

        ### Notes
        1. 颜色值可以使用Colors枚举，如Colors.red, Colors.green
        2. 也可以使用十六进制颜色代码，如'#FF0000'（红色）
        3. 颜色设置应与交易逻辑一致，便于快速识别
        4. 建议遵循行业惯例：绿色=买入，红色=卖出

        Examples:
        ```python
        # 设置买入信号为绿色
        self.test.signal_color.long_signal = Colors.green

        # 设置卖出信号为红色
        self.test.signal_color.short_signal = Colors.red

        # 使用十六进制颜色
        self.test.signal_color.exit_signal = '#FFA500'  # 橙色

        # 使用RGB颜色
        from matplotlib.colors import to_hex
        self.test.signal_color.entry_signal = to_hex((0, 0.5, 1))  # 蓝色

        # 查看当前颜色设置
        print(f"买入信号颜色: {self.test.signal_color.long_signal}")
        ```

        ## Setter: 统一设置所有信号的颜色
        ```python
        # 将所有信号设置为红色
        self.test.signal_color = Colors.red

        # 将所有信号设置为蓝色
        self.test.signal_color = Colors.blue

        # 使用自定义颜色
        self.test.signal_color = '#32CD32'  # 石灰绿色
        ```
        """
        return SignalAttrType(self, "color")

    @signal_color.setter
    def signal_color(self, value):
        """## 设置所有信号线的统一颜色"""
        self._plotinfo.signal_color = value

    @property
    def signal_overlap(self) -> SignalAttrType:
        """## 信号主图重叠显示设置

        - 控制信号标记是否覆盖在主K线图上显示。
        - 当设置为False时，信号标记会在副图指标中显示，默认是主图叠加。

        Returns:
        >>> SignalAttrType
            信号重叠显示配置对象，用于设置信号的显示模式

        ### Notes
        1. overlap=True: 信号标记覆盖在主K线图上（默认）
        2. overlap=False: 信号在独立面板中显示
        3. 独立显示可以避免信号标记与价格线互相遮挡
        4. 当信号较多时，建议使用独立显示以提高图表清晰度

        Examples:
        ```python
        # 买入信号在主图上显示（与价格线重叠）
        self.test.signal_overlap.long_signal = True

        # 卖出信号在独立面板显示
        self.test.signal_overlap.short_signal = False

        # 平仓信号在主图上显示
        self.test.signal_overlap.exit_signal = True

        # 查看当前设置
        print(f"买入信号重叠: {self.test.signal_overlap.long_signal}")
        print(f"卖出信号重叠: {self.test.signal_overlap.short_signal}")
        ```

        ## Setter: 统一设置所有信号的重叠显示模式
        ```python
        # 所有信号都在主图上显示（默认）
        self.test.signal_overlap = True

        # 所有信号都在独立面板显示
        self.test.signal_overlap = False

        # 这种设置适合信号较多或需要清晰查看价格走势的情况
        ```
        """
        return SignalAttrType(self, "overlap")

    @signal_overlap.setter
    def signal_overlap(self, value):
        """## 设置所有信号线的统一重叠显示模式"""
        self._plotinfo.signal_overlap = value

    @property
    def signal_size(self) -> SignalAttrType:
        """## 信号标记大小设置

        - 设置信号标记在图表上的显示大小，默认为12。
        - 较大的标记更醒目，但可能遮挡更多价格信息。

        Returns:
        >>> SignalAttrType
            信号大小配置对象，用于设置信号标记的尺寸

        ### Notes
        1. 大小值通常为整数，表示标记的像素尺寸
        2. 常用范围：10-50，默认为12
        3. 不同类型的信号可以使用不同的大小以示区分
        4. 大小设置应考虑图表比例，避免标记过大或过小

        Examples:
        ```python
        # 设置买入信号标记大小为30
        self.test.signal_size.long_signal = 30

        # 设置卖出信号标记大小为25
        self.test.signal_size.short_signal = 25

        # 设置平仓信号标记大小为20
        self.test.signal_size.exit_signal = 20

        # 重要信号使用更大的标记
        self.test.signal_size.strong_buy = 40
        self.test.signal_size.strong_sell = 40

        # 查看当前设置
        print(f"买入信号大小: {self.test.signal_size.long_signal}")
        ```

        ## Setter: 统一设置所有信号标记的大小
        ```python
        # 将所有信号标记设置为相同大小
        self.test.signal_size = 25  # 默认大小

        # 使用较大标记（适用于演示或报告）
        self.test.signal_size = 35

        # 使用较小标记（信号密集时）
        self.test.signal_size = 15
        ```
        """
        return SignalAttrType(self, "size")

    @signal_size.setter
    def signal_size(self, value):
        """## 设置所有信号线的统一标记大小"""
        self._plotinfo.signal_size = value

    @property
    def signal_label(self) -> SignalAttrType:
        """## 信号标签设置

        - 设置信号标记在图表上显示的文本标签。
        - 标签可以用于标识信号类型或添加额外信息。

        Returns:
        >>> SignalAttrType
            信号标签配置对象，用于设置信号标记的文本标签

        ### Notes
        1. 标签文本会显示在信号标记旁边
        2. 可以用于显示信号名称、交易数量、盈亏比例等信息
        3. 标签支持格式化字符串，可以包含动态数据
        4. 过多的标签可能使图表杂乱，应谨慎使用

        Examples:
        ```python
        # 设置简单的文本标签
        self.test.signal_label.long_signal = SignalLabel("买入",size=10, style="bold", color="red")
        self.test.signal_label.short_signal = SignalLabel("卖出",size=10, style="bold", color="red")

        # 清空标签（不显示）
        self.test.signal_label.exitlong_signal = False

        # 查看当前设置
        print(f"买入信号标签: {self.test.signal_label.long_signal}")
        ```

        ## Setter: 统一设置所有信号的标签
        ```python
        # 为所有信号设置相同的标签前缀
        self.test.signal_label = SignalLabel("Signal")

        # 清空所有信号的标签
        self.test.signal_label = False

        # 注意：统一设置会覆盖之前的个性化设置
        ```
        """
        return SignalAttrType(self, "label")

    @signal_label.setter
    def signal_label(self, value):
        """## 设置所有信号线的统一标签"""
        self._plotinfo.signal_label = value

    @property
    def line_style(self) -> LineStyleType:
        """## 指标线型样式配置

        - 用于配置指标线的线型样式，支持以下两种使用方式：

        1. **链式调用**：设置特定指标线的完整样式
        2. **统一设置**：设置所有指标线的统一样式

        Returns:
        >>> LineStyleType: 线型样式配置对象，支持链式调用设置单个指标线样式

        Examples:
        ```python
        # 链式调用设置特定指标线样式
        indicator.line_style.long_signal = LineStyle(
            dash=LineDash.dashdot,
            width=3,
            color=Colors.red
        )

        # 统一设置所有指标线样式
        indicator.line_style = LineStyle(
            dash=LineDash.dashdot,
            width=3,
            color=Colors.red
        )
        ```
        """
        return LineStyleType(self)

    @line_style.setter
    def line_style(self, value):
        """## 统一设置指标线样式

        Args:
            value (LineStyle): 线型样式对象，包含dash、width、color属性

        Note:
            - 仅对指标实例生效
            - 对于多维指标(isMDim=True)，会遍历设置所有子线样式
        """
        if self.isindicator and isinstance(value, LineStyle):
            self._plotinfo.line_style = value
            if self._indsetting.isMDim:
                for line in self.line:
                    line._plotinfo.line_style = value

    @property
    def line_dash(self) -> LineAttrType:
        """## 指标线虚线样式配置

        - 用于配置指标线的虚线样式，支持以下两种使用方式：

        1. **链式调用**：设置特定指标线的虚线样式
        2. **统一设置**：设置所有指标线的统一虚线样式

        Returns:
        >>> LineAttrType: 线型属性配置对象，支持链式调用设置单个属性

        ### 示例:
        ```python
        # 链式调用设置特定指标线虚线样式
        indicator.line_dash.long_signal = LineDash.solid

        #  统一设置所有指标线虚线样式
        indicator.line_dash = LineDash.dashdot
        ```
        ### Note: 
        >>> 支持样式：solid(实线), dash(虚线), dot(点线), dashdot(点划线)
        """
        return LineAttrType(self, "line_dash")

    @line_dash.setter
    def line_dash(self, value):
        """## 统一设置指标线虚线样式

        Args:
            value (LineDash): 虚线样式枚举值

        Note:
            - 仅对指标实例生效
            - 对于多维指标(isMDim=True)，会遍历设置所有子线样式
        """
        if self.isindicator:
            self._plotinfo.line_dash = value
            if self._indsetting.isMDim:
                for line in self.line:
                    line._plotinfo.line_dash = value

    @property
    def line_width(self) -> LineAttrType:
        """## 指标线宽度配置

        - 用于配置指标线的宽度，支持以下两种使用方式：

        1. **链式调用**：设置特定指标线的宽度
        2. **统一设置**：设置所有指标线的统一宽度

        Returns:
        >>> LineAttrType: 线型属性配置对象，支持链式调用设置单个属性

        ### 示例:
        ```python
        # 链式调用设置特定指标线宽度
        indicator.line_width.long_signal = 3

        # 统一设置所有指标线宽度
        indicator.line_width = 3
        ```
        ### Note: 
        - 宽度值应为正整数，表示像素宽度
        - 该方法仅在指标实例中有效
        """
        return LineAttrType(self, "line_width")

    @line_width.setter
    def line_width(self, value):
        """## 统一设置指标线宽度

        Args:
            value (int): 线宽值，单位为像素

        Note:
            - 仅对指标实例生效
            - 对于多维指标(isMDim=True)，会遍历设置所有子线样式
        """
        if self.isindicator:
            self._plotinfo.line_width = value
            if self._indsetting.isMDim:
                for line in self.line:
                    line._plotinfo.line_width = value

    @property
    def line_color(self) -> LineAttrType:
        """## 指标线颜色配置

        - 用于配置指标线的颜色，支持以下两种使用方式：

        1. **链式调用**：设置特定指标线的颜色
        2. **统一设置**：设置所有指标线的统一颜色

        Returns:
        >>> LineAttrType: 线型属性配置对象，支持链式调用设置单个属性
        ```python
        # 链式调用设置特定指标线颜色
        indicator.line_color.long_signal = Colors.red

        # 统一设置所有指标线颜色
        indicator.line_color = Colors.red
        ```
        ### Note: 
        - 颜色值应为Colors枚举值或RGB颜色值
        - 该方法仅在指标实例中有效
        """
        return LineAttrType(self, "line_color")

    @line_color.setter
    def line_color(self, value):
        """## 统一设置指标线颜色

        Args:
            value (Colors): 颜色值，支持Colors枚举或RGB格式

        Note:
            - 仅对指标实例生效
            - 对于多维指标(isMDim=True)，会遍历设置所有子线样式
        """
        if self.isindicator:
            self._plotinfo.line_color = value
            if self._indsetting.isMDim:
                for line in self.line:
                    line._plotinfo.line_color = value

    @property
    def price_line(self) -> LineAttrType:
        """## 指标线价格水平线显示配置

        - 只适配lightweight_charts图表
        - 用于配置指标线/直方图是否显示价格水平线（在图表中指标价格水平线）
        - 支持两种使用方式，适配不同的配置场景：

        1. **链式调用**：为特定指标线单独设置价格标签显示状态
        2. **统一设置**：为当前指标实例下所有线统一设置价格标签显示状态

        Returns:
            LineAttrType: 线型属性配置对象，支持链式调用设置单个属性值

        ```python
        # 链式调用：为long_signal指标线开启价格水平线显示
        indicator.price_line.long_signal = True

        # 统一设置：为当前指标所有线关闭价格水平线显示
        indicator.price_line = False
        """
        return LineAttrType(self, "price_line")

    @price_line.setter
    def price_line(self, value):
        if self.isindicator:
            self._plotinfo.price_line = value
            if self._indsetting.isMDim:
                for line in self.line:
                    line._plotinfo.price_line = value

    @property
    def price_label(self) -> LineAttrType:
        """## 指标线价格标签显示配置

        - 只适配lightweight_charts图表
        - 用于配置指标线/直方图是否显示价格标签（在图表中指标数值旁显示价格文本）
        - 支持两种使用方式，适配不同的配置场景：

        1. **链式调用**：为特定指标线单独设置价格标签显示状态
        2. **统一设置**：为当前指标实例下所有线统一设置价格标签显示状态

        Returns:
            LineAttrType: 线型属性配置对象，支持链式调用设置单个属性值

        ```python
        # 链式调用：为long_signal指标线开启价格标签显示
        indicator.price_label.long_signal = True

        # 统一设置：为当前指标所有线关闭价格标签显示
        indicator.price_label = False
        """
        return LineAttrType(self, "price_label")

    @price_label.setter
    def price_label(self, value):
        if self.isindicator:
            self._plotinfo.price_label = value
            if self._indsetting.isMDim:
                for line in self.line:
                    line._plotinfo.price_label = value

    def to_lines(self, *args: int | str) -> tuple[Line]:
        """### 返回多列Line"""
        if not args:
            args = self._plotinfo.line_filed
        else:
            assert all([isinstance(arg, (int, str))
                        for arg in args]), f"参数为整数或字符串并在{self._plotinfo.line_filed}中"
        return (getattr(self, arg if isinstance(arg, str) else self._plotinfo.line_filed) for arg in args)

    def to_ndarray(self) -> tuple[np.ndarray]:
        """### 返回多列np.ndarray"""
        return (line.values for line in self.to_lines())

    # 特殊函数
    def select_dtypes(self, include: Literal["int", "float", "number", "object", "datetime", "bool"] | list[str] = None, exclude=None, **kwargs) -> IndFrame | IndSeries:
        """## 根据数据类型选择列

        - 基于列的数据类型筛选DataFrame的列。

        Args:
            `include`：指定要保留的列类型（可选）
                - 含义：传入需要筛选保留的数据类型，列的 dtype 匹配该参数时会被保留。
                - 支持的传入格式：
                - 单个 dtype 字符串（如 `'int64'`、`'object'`）
                - dtype 列表（如 `['int64', 'float64']`）
                - numpy dtype 对象（如 `np.int64`、`np.float32`）
                - 快捷类型别名（如 `'number'` 代表所有数值类型：int + float；`'datetime'` 代表日期时间类型）
            `exclude`：指定要排除的列类型（可选）
                - 含义：传入需要过滤排除的数据类型，列的 dtype 匹配该参数时会被剔除。
                - 传入格式：与 `include` 完全一致（单个值、列表、numpy dtype 均可）。

        ### 注意事项
        - `include` 和 `exclude` 可以同时使用（先按 `include` 筛选，再从结果中按 `exclude` 排除）。
        - 两者不能同时为 `None`（默认 `include=None`、`exclude=None`，此时会抛出异常）。
        - 不能传入互相矛盾的参数（如 `include='int64'` 同时 `exclude='int64'`）。


        ### 常用数据类型参考

        | 类型类别       | 具体 dtype                | 快捷别名       |
        |----------------|--------------------------|----------------|
        | 整数类型       | int64、int32、int16       | 'int'          |
        | 浮点类型       | float64、float32          | 'float'        |
        | 数值类型（通用）| int + float               | 'number'       |
        | 字符串/对象类型 | object                   | 'object'       |
        | 日期时间类型   | datetime64[ns]            | 'datetime'     |
        | 布尔类型       | bool                      | 'bool'         |

        Returns:
        >>> IndFrame: 筛选后的数据子集

        Examples:
            >>> df = IndFrame({'A': [1, 2], 'B': ['x', 'y'], 'C': [1.1, 2.2]})
            >>> result = df.select_dtypes(include='number')
            >>> print(result)
            A    C
            0  1  1.1
            1  2  2.2

            >>> # 更多示例
            >>> df.select_dtypes(include=['int', 'float64'])  # 整数和浮点数
            >>> df.select_dtypes(exclude=['object', 'datetime'])  # 排除对象和日期类型
            >>> df.select_dtypes(include=['Int64', 'Float64'])  # 可空数值类型
        """
        # 调用 pandas 的 select_dtypes
        pandas_result = super().select_dtypes(include=include, exclude=exclude, **kwargs)

        if not options.check_conversion_mode(pandas_result, self):
            return pandas_result
        isMDim = pandas_result.ndim == 2
        # 转换为 IndFrame
        indicator_kwargs = self.get_indicator_kwargs(**kwargs)
        indicator_kwargs["lines"] = list(
            pandas_result.columns) if isMDim else [self.sname,]
        if isMDim:
            return IndFrame(pandas_result.values, **indicator_kwargs)
        else:
            return IndSeries(pandas_result.values, **indicator_kwargs)

    def pop(self, item: Hashable, **kwargs) -> IndSeries | None:
        """## 弹出指定列

        - 从DataFrame中删除并返回指定列。

        Args:
            item: 要弹出的列名

        Returns:
        >>> IndSeries: 弹出的列数据

        Examples:
            >>> df = IndFrame({'A': [1, 2], 'B': [3, 4]})
            >>> popped = df.pop('B')
            >>> print(popped)
            0    3
            1    4
            Name: B, dtype: int64
            >>> print(df)
               A
            0  1
            1  2
        """
        if item in self.lines:
            pandas_result = self.pandas_object.pop(item=item, **kwargs)

            if not options.check_conversion_mode(pandas_result, self):
                return pandas_result
            selected_lines = self._plotinfo.lines.values
            selected_lines.remove(item)
            p1, p2 = self._plotinfo.split_by_lines(
                selected_lines, True)
            data = IndFrame(self.pandas_object.values, **p1.vars)
            data._dataset.source_object = self._dataset.source_object
            self.__dict__.update(data.__dict__)
            result = IndSeries(pandas_result.values, **p2.vars)
            result._dataset.source_object = self._dataset.source_object
            return result

    @property
    def entries(self) -> IndSeries | None:
        """### 获取开仓信号

        Returns:
            IndSeries: 开仓信号
        """
        long_signal = self.long_signal
        short_signal = self.short_signal
        if long_signal is None or short_signal is None:
            return
        entries = long_signal-short_signal
        return entries

    @property
    def exits(self) -> IndSeries | None:
        """### 获取平仓信号

        Returns:
            IndSeries: 平仓信号
        """
        exitlong_signal = self.exitlong_signal if self.exitlong_signal is not None  and not self.exitlong_signal.empty else self.short_signal
        exitshort_signal = self.exitshort_signal if self.exitshort_signal is not None and not self.exitshort_signal.empty else self.long_signal
        if exitlong_signal is None or exitshort_signal is None:
            return
        exits = -exitlong_signal+exitshort_signal
        return exits

    @property
    def iloc(self):
        """
        ## 基于**整数位置**的索引器（按行/列的位置序号选择数据）

        ### 核心特点：
        - 位置从 0 开始（如第 1 行对应 0，第 2 列对应 1）
        - 支持切片、整数列表、布尔数组等输入（详见示例）
        - 越界索引会报错（切片除外，兼容 Python 切片语义）

        ### 允许的输入类型：
        - 1. 单个整数（如 5 → 选择第 6 行/列）
        - 2. 整数列表/数组（如 [4,3,0] → 按顺序选择第 5、4、1 行/列）
        - 3. 切片（如 1:7 → 选择第 2 到第 7 行/列，包含边界）
        - 4. 布尔数组（长度需与行/列数一致，True 表示选择）
        - 5. 元组（如 (0,1) → 选择第 1 行第 2 列）

        ### 返回值：
        - 框架内置指标数据（单个值/`IndSeries`/`IndFrame`）
        - 原生 Pandas 对象

        ### 示例：
        >>> df.iloc[0]          # 选择第 1 行（返回 IndSeries 类型）
        >>> df.iloc[[0,2], [1]] # 选择第 1、3 行，第 2 列（返回 IndFrame 类型）
        """
        return MinibtILocIndexer("iloc", self)

    @property
    def loc(self):
        """
        ## 基于**标签（名称）** 的索引器（按行/列的标签选择数据）

        ### 核心特点：
        - 依赖索引标签（如行标签 'cobra'、列标签 'max_speed'）
        - 切片包含首尾边界（与 Python 原生切片不同）
        - 支持条件筛选（如按列值过滤行）

        ### 允许的输入类型：
        - 1. 单个标签（如 'a' 或 5 → 选择标签为 'a'/5 的行/列）
        - 2. 标签列表/数组（如 ['a','b'] → 按顺序选择指定标签）
        - 3. 标签切片（如 'a':'f' → 选择标签从 'a' 到 'f' 的行/列，包含边界）
        - 4. 布尔数组/Series（长度/索引需与行/列匹配）
        - 5. 函数（输入为当前对象，返回上述合法输入）

        ### 返回值：
        - 框架内置指标数据（单个值/`IndSeries`/`IndFrame`）
        - 原生 Pandas 对象
        """
        return MinibtLocIndexer("loc", self)

    # ------------------------------
    # 索引访问重载（支持[]操作符）
    # ------------------------------
    def __getitem__(self, key) -> KLine | IndFrame | IndSeries | Line:
        """
        ## 重载索引访问（[]操作符，内部方法）
        - 支持通过索引/字段名获取数据，自动将返回的Pandas对象转为框架内置指标，
        - 确保索引操作后仍保持指标对象的一致性（如属性、配置、运算能力）。

        ### 支持的索引类型：
        - 索引整数（如self.data[0] → 第一行数据）
        - 负数整数（如self.data[-1] → 最后一行数据）
        - 切片（如self.data[10:20] → 第11-20行数据）
        - 负数切片（如self.data[-10:] → 最后10行数据）
        - 字符串（如self.data["close"] → "close"字段）
        - 布尔数组（如self.data[self.data.close > 10000] → 收盘价>10000的行）
        - 元组（如self.data[:, "close"] → 所有行的"close"字段）

        Args:
            key (int | slice | str | np.ndarray | tuple): 索引键

        Returns:
            IndFrame | IndSeries | Line | Any:
                - 若返回Pandas对象（DataFrame/Series）→ 转为框架内置指标
                - 若返回标量/其他类型 → 直接返回

        ### 核心逻辑：
        - 1. 调用父类索引方法：获取原始结果（Pandas对象/标量）
        - 2. 结果转换：若为Pandas对象，调用pandas_to_btind转为内置指标，
        继承当前指标的配置（ID、长度、绘图信息），确保属性一致
        - 3. 特殊处理：元组索引（如多维数据的行列选择）时，提取正确的字段名，
        确保线条名称与原始字段匹配
        """
        data = None

        # 1. 处理整数索引（正/负）：按行位置取值，使用iloc
        if isinstance(key, int):
            # 适配框架内部btindex，处理偏移逻辑
            if key < 0:
                key += self.btindex + 1
            # 整数作为行索引，调用iloc取值
            data = self.iloc[key]

        # 2. 处理元组索引（多维索引，如 [行切片, 列名]）
        elif isinstance(key, tuple):
            data = self.loc[key]

        # 3. 处理切片（重点：完善负数切片逻辑）
        elif isinstance(key, slice):
            # 提取切片的start/stop/step，处理None值（默认值）
            start = key.start
            stop = key.stop
            step = key.step if key.step is not None else 1

            # 标记是否需要转换负数索引
            need_convert = False
            new_start = start
            new_stop = stop

            # 处理负数start
            if isinstance(start, int) and start < 0:
                new_start = self.btindex + 1 + start
                need_convert = True

            # 处理负数stop
            if isinstance(stop, int) and stop < 0:
                new_stop = self.btindex + 1 + stop
                need_convert = True

            # 情况1：有负数索引 → 用iloc+转换后的正数切片
            if need_convert:
                # 构建新的切片对象（替换负数为正数）
                new_slice = slice(new_start, new_stop, step)
                data = self.iloc[new_slice]
            # 情况2：无负数索引 → 沿用父类逻辑
            else:
                data = super().__getitem__(key)

        # 4. 其他类型：字符串/布尔数组等，沿用父类逻辑
        else:
            data = super().__getitem__(key)

        # 5. 转换Pandas对象为框架内置指标
        # __getitem__ 需要总是转换以保持类型一致性
        if ispandasojb(data):
            indicator_kwargs = self.get_indicator_kwargs()
            # 根据数据维度转换为相应的minibt数据结构
            if len(data.shape) > 1:  # 多维数据
                if type(self) in KLineType and set(data.columns).issuperset(FILED.ALL):
                    data = data.add_info(**self._klinesetting.symbol_info)
                    return KLine(data, **indicator_kwargs)
                else:
                    indicator_kwargs["lines"] = list(data.columns)
                    return IndFrame(data, **indicator_kwargs)
            else:  # 一维数据
                return IndSeries(data, **indicator_kwargs)
        return data

    def __setattr__(self, name, value):
        # 私有属性使用标准赋值
        if name.startswith('_'):
            object.__setattr__(self, name, value)
            return
        # 可迭代值（排除 str/bytes/dict/Cache/DataSetBase）通过 __setitem__ 路由为列赋值
        # DataSetBase(LineStyle/CandleStyle/SignalStyle/SpanStyle 等)虽有 __iter__
        # 但它们是配置对象而非数据容器，应作为属性存储而非列赋值
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict, Cache, DataSetBase)):
            self[name] = value
        else:
            object.__setattr__(self, name, value)

    def __setitem__(self, key, value):
        """
        ## 重载索引赋值（[]=操作符，内部方法）
        - 支持通过索引/字段名修改数据，同步更新底层Pandas对象与上采样数据

        Args:
            key: 索引键
            value: 待赋值的数据
        """
        ischange = key not in self.lines
        # 安全获取 value 的 kline（避免 value.kline 内部遍历 source_object 链时崩溃）
        try:
            source_obj = value.kline if hasattr(value, 'kline') else None
        except Exception:
            source_obj = None
        # pandas 3.0 严格要求类型匹配，先转换 value 类型
        if hasattr(value, '__iter__'):
            src_dtype = getattr(self, 'dtype', None)
            value = np.asarray(value, dtype=src_dtype)
        if self.isMDim:
            pd.DataFrame.__setitem__(self, key, value)
            self.pandas_object.loc[:, key] = value
        else:
            pd.Series.__setitem__(self, key, value)
            self.pandas_object.loc[key] = value
        # 3. 上采样数据更新：若存在上采样数据，重置并重新计算
        if self.strategy_instances and self._dataset.upsample_object is not None:
            if self.sid in self.strategy_instances:
                strategy_instance = self.strategy_instances[self.sid]
                setattr(strategy_instance, self._upsample_name,
                        self.upsample(reset=True))
        if not self.isMDim:
            self._dataset.set_copy_obj(self)
            if source_obj is not None:
                self._dataset.set_source_obj(source_obj)
            return
        data = self.values
        if ischange:
            self._plotinfo.lines = Lines(*self.pandas_object.columns)(self)
            self._plotinfo.line_filed = list(
                map(lambda x: f"_{x}", self.lines))
            if key in SIGNAL_Str:
                self._plotinfo.signallines.append(key)
                self._plotinfo.signalstyle.update(
                    {key: default_signal_style(key)})
            self._plotinfo.line_style.update({key: LineStyle()})
            # isplot / overlap 在列数 ≤1 时为 bool，新增列时需转为 dict
            isplot = True
            overlap = False
            if isinstance(self._plotinfo.isplot, bool):
                isplot=self._plotinfo.isplot
                self._plotinfo.isplot = Addict()
                
            if isinstance(self._plotinfo.overlap, bool):
                overlap=self._plotinfo.overlap
                self._plotinfo.overlap = Addict()
            self._plotinfo.isplot[key] = isplot
            self._plotinfo.overlap[key] = overlap
            

            for i, line_field in enumerate(self._plotinfo.line_filed):
                try:
                    line_obj: Line = getattr(self, line_field)
                    line_obj[:] = data[:, i]
                except:
                    new_line = self._Line(
                        self, data[:, i], iscustom=self.iscustom, id=self.id.copy(), sname=self.lines[i],
                        ind_name=self.ind_name, lines=[
                            self.lines[i],], category=Category.Any,
                        isplot=True, ismain=self.ismain, isreplay=self.isreplay,
                        isresample=self.isresample, overlap=False)
                    object.__setattr__(self, line_field, new_line)
                    set_property(self.__class__, self.lines[i])
        else:
            for i, line_field in enumerate(self._plotinfo.line_filed):
                line_obj: Line = getattr(self, line_field)
                line_obj[:] = data[:, i]
        self._dataset.set_copy_obj(self)
        if source_obj is not None:
            self._dataset.set_source_obj(source_obj)

    def signal_backtest(self, 
                        size: float = 1.0, 
                        size_type: SizeType | None = None,
                        commission: float | None = None, 
                        com_type: int | CommissionType | None = None, 
                        slip_point: float | None = None,
                        prices: np.ndarray | IndSeries | None = None, 
                        init_cash: float = 1000000.0,
                        min_start_length: int | None = None,
                        isplot: bool = True,
                        isreport: bool = True,
                        sl_stop: float = 0.0,
                        tp_stop: float = 0.0,
                        stop_mode: Literal[0, 1, 2] = 0,
                        sl_trail: Literal[0, 1] = 0,
                        sl_callback: Callable | None = None,
                        tp_callback: Callable | None = None,
                        sl_callback_args: tuple = (),
                        tp_callback_args: tuple = (),
                        max_hold_bars: int = 0,
                        optimize: bool | OptimizeConfig = False,
                        optimize_weights: float | tuple[float, ...] | None = None,
                        **kwargs) -> Strategy | SignalBacktestResult | OptimizationResult | None:
        """
        ## 基于信号进行快速Cython回测（无需完整策略流程）

        使用IndFrame内置的交易信号（``long_signal``/``short_signal``/``exitlong_signal``/``exitshort_signal``），
        通过Cython加速的 ``from_signals`` 引擎直接模拟逐笔交易，大幅提升回测速度。

        ### 信号映射规则：
        - **entries** = ``long_signal - short_signal``：>0 为多头信号，<0 为空头信号
        - **exits** = ``-exitlong_signal + exitshort_signal``：<0 为多头离场，>0 为空头离场
          （即 exitlong_signal 触发后，cython 引擎用 -1 平多仓；exitshort_signal 用 +1 平空仓）

        ### 与策略回测的区别：
        - 无需继承 Strategy 类，无需定义 next()/step() 方法
        - 不依赖 Broker、Account 等完整框架组件，直接对信号数据进行回测
        - 底层使用 Cython 编译的 from_signals 函数，运行速度更快
        - 返回轻量级结果对象，内置统计分析、格式化打印与 QuantStats 绘图功能

        ### Args:

        | 参数 | 类型 | 默认值 | 说明 |
        |---|---|---|---|
        | `size` | `float` | `1.0` | 交易规模，含义取决于 `size_type` |
        | `size_type` | `SizeType | None` | `None` | 规模类型：`SizeType.Amount` (0) 按手数 / `SizeType.Value` (1) 按固定金额 / `SizeType.Percent` (2) 按账户资金百分比 |
        | `commission` | `float` | `1.0` | 手续费/佣金，含义取决于 `com_type` |
        | `com_type` | `CommissionType | None` | `None` | 手续费类型：`CommissionType.Tick` (0) 按 tick 值 / `CommissionType.Fixed` (1) 固定金额 / `CommissionType.Percent` (2) 按成交额百分比 |
        | `slip_point` | `float` | `0.0` | 滑点（价格偏移点数） |
        | `prices` | `np.ndarray | IndSeries | None` | `None` | 成交价序列，默认使用收盘价 close |
        | `init_cash` | `float` | `1000000.0` | 初始资金 |
        | `min_start_length` | `int | None` | `None` | 最小启动K线数（在此之前不交易），默认取自关联KLine的 `_klinesetting` |
        | `isplot` | `bool` | `True` | 是否绘制回测图表 |
        | `isreport` | `bool` | `True` | 是否生成回测报告 |
        | `sl_stop` | `float` | `0.0` | 止损值，0 表示不使用止损，含义取决于 `stop_mode` |
        | `tp_stop` | `float` | `0.0` | 止盈值，0 表示不使用止盈，含义取决于 `stop_mode` |
        | `stop_mode` | `Literal[0,1,2]` | `0` | 止损/止盈模式：`0` 按最小波动单位 / `1` 按金额 / `2` 按合约价值百分比 |
        | `sl_trail` | `Literal[0,1]` | `0` | 是否启用移动止损：`0` 不启用（止损线固定） / `1` 启用（随行情向有利方向移动，永不后退） |
        | `sl_callback` | `Callable | None` | `None` | 动态止损回调函数，需为 `numba.cfunc` 编译的 C 函数指针。签名：`double(int64,int64,float64,float64,float64,float64,float64,float64,float64,float64[:],int64)`，返回新止损距离（>0 更新，<=0 保持不变）。`args_data` 为 `sl_callback_args` 展平的一维 float64 数组，`args_count` 为其长度。不提供则使用 `sl_stop` 固定值模式 |
        | `tp_callback` | `Callable | None` | `None` | 动态止盈回调函数，同 `sl_callback`，影响止盈而非止损 |
        | `sl_callback_args` | `tuple` | `()` | 止损回调附加参数。每个元素可为标量或与信号同长的 `np.ndarray`，自动展平为 float64 一维数组通过 `args_data`/`args_count` 传递。例如 `(base_sl, multiplier_arr)` 展平为 `[base_sl, arr[0], arr[1], ...]`，回调中 `args_data[1+bar_idx]` 取第 bar_idx 个乘数 |
        | `tp_callback_args` | `tuple` | `()` | 止盈回调附加参数，对标 vectorbt 的 `adjust_tp_args` |
        | `max_hold_bars` | `int` | `0` | 持仓最大K线根数。>=1 时持仓达该值强制以当前市价离场，与其他止损/止盈并列（谁先触发谁执行）。0=禁用 |
        | `optimize` | `bool | OptimizeConfig` | `False` | 是否启用参数优化：`False` 策略构建模式（完整回测+可视化/报告） / `True` 参数优化模式（通过 `**kwargs` 传参） / `OptimizeConfig` 实例（推荐，统一管理 params/target/method/weights/config/n_jobs/output_dir） |
        | `optimize_weights` | `float | tuple[float,...] | None` | `None` | 优化目标权重。`None` 时所有目标最大化且等权重 1.0。正值最大化目标（如 1.0），负值最小化目标（如 -1.0）。元组用于多目标优化与 `optimize_target` 一一对应。若 `optimize` 为 `OptimizeConfig` 实例则优先使用实例中的 `weights` |
        | ``**kwargs`` | - | - | 见下方 **Kwargs** |

        ### Kwargs:
        > 仅当 ``optimize=True`` 且未传入 ``OptimizeConfig`` 实例时生效。

        | 参数 | 类型 | 默认值 | 说明 |
        |---|---|---|---|
        | `optimize_params` | `dict[str, tuple|list]` | **必需** | 待优化参数与取值范围，如 `{'length': (5,50,5), 'mult': [1.5,2.0,2.5]}` |
        | `optimize_target` | `str | list[str]` | `'sharpe'` | 优化目标指标名，支持所有 `_calc_qs_metrics` 指标（sharpe/sortino/max_drawdown/calmar 等） |
        | `optimize_method` | `str` | `'optuna'` | 优化方法：`'optuna'` 贝叶斯优化 / `'grid'` 网格搜索 |
        | `optimize_config` | `dict | None` | `None` | 优化器配置。通用：`{'n_trials': 200, 'n_jobs': 4}`；Optuna：`{'sampler': 'TPESampler', 'pruner': 'MedianPruner'}` |
        | `n_jobs` | `int` | `1` | 并行线程数，`-1` 使用全部核心 |
        | `output_dir` | `str | None` | `None` | CSV 结果导出目录，默认不导出 |

        ### Returns:
        SignalBacktestResult: 回测结果容器对象，提供以下接口：
        - ``.profits``: 权益曲线（pd.Series）
        - ``.result_df``: 逐笔回测明细 DataFrame
        - ``.stats``: Stats 统计分析对象
        - ``.qs_plots``: QSPlots 绘图对象
        - ``.pprint()``: 格式化打印核心统计指标
        - ``.plot()``: 返回 QSPlots 对象供绘制收益曲线
        - ``.report(output, show)``: 生成 QuantStats HTML 分析报告
        - ``.total_fee`` / ``.total_profit``: 总手续费 / 总盈亏

        ### Raises:
        ValueError: 未设置交易信号或无法自动找到关联 KLine 时抛出

        ### Example:
            >>> # 1. 基于K线计算MA指标
            >>> ind = kline.ma(20)
            >>> # 2. 设置交易信号
            >>> ind.long_signal = ind.ma20.cross_up(kline.close)
            >>> ind.short_signal = ind.ma20.cross_down(kline.close)
            >>> # 3. 快速回测
            >>> result = ind.signal_backtest(kline, commission=5, size=1)
            >>> result.pprint()   # 打印核心统计指标
            >>> result.plot()     # 获取绘图对象（可进一步自定义）
            >>> result.report()   # 生成HTML分析报告
        """
        kline = None
        for sig_name in ('_long_signal', '_short_signal',
                            '_exitlong_signal', '_exitshort_signal'):
            sig = getattr(self, sig_name, None)
            if sig is not None and sig.kline is not None:
                kline = sig.kline
                break
        if kline is None:
            raise ValueError(
                "无法自动找到关联的KLine对象。请通过 kline 参数显式指定：\n"
                "  result = ind.signal_backtest(kline=my_kline)"
            )
        # -------------------------- 1. 参数默认值 --------------------------
        from ..other import SizeType, CommissionType
        if size_type is None:
            size_type = SizeType.Amount
        # 从K线数据获取费用参数（未显式设置时）
        if commission is None:
            commission = float(kline.commission or 0.0)
        if com_type is None:
            ct = kline.commission_type
            com_type = int(ct) if ct is not None else int(CommissionType.Fixed)
        if slip_point is None:
            sp = kline.slip_point
            slip_point = float(sp) if sp is not None else 0.0

        # -------------------------- 2. 获取信号数据 --------------------------
        long_sig = self.long_signal
        short_sig = self.short_signal
        exitlong_sig = self.exitlong_signal
        exitshort_sig = self.exitshort_signal

        if long_sig is None and short_sig is None:
            raise ValueError(
                "请先设置 long_signal 和/或 short_signal 信号。\n"
                "示例: ind.long_signal = ind.ma5.cross_up(ind.ma10)"
            )

        # entries = long_signal - short_signal
        #   >0 → 多头(1), <0 → 空头(-1), ==0 → 无信号
        ntries= None
        if short_sig is None:
            entries = long_sig
        elif long_sig is None:
            entries = -short_sig
        else:
            entries = long_sig - short_sig

        # exits = -exitlong_signal + exitshort_signal
        #   <0(-1) → 平多头, >0(+1) → 平空头, ==0 → 无离场
        exits = None
        if exitlong_sig is None:
            exits = exitshort_sig
        elif exitshort_sig is None:
            exits = -exitlong_sig
        else:
            exits = -exitlong_sig + exitshort_sig
        if exits is None:
            # exitlong_sig = short_sig
            # exitshort_sig = long_sig
            # exits = -short_sig + long_sig
            exits= entries

        # ---- 检测 entries/exits 是否包含有效交易信号 ----
        def _has_valid_signals(arr):
            """判断信号数组是否包含非零值（即存在实际交易信号）"""
            if arr is None:
                return False
            vals = arr.values if hasattr(arr, 'values') else np.asarray(arr)
            return np.any(vals != 0)

        if not _has_valid_signals(entries):
            raise ValueError(
                "指标未产生任何有效的交易信号（entries 全为零），无法进行回测。\n"
                "请检查信号条件是否正确设置，例如：\n"
                "  ind.long_signal = ind.ma5.cross_up(ind.ma10)\n"
                "  ind.short_signal = ind.ma5.cross_down(ind.ma10)"
            )
        
        # margin_rate = float(kline.margin_rate) if kline.margin_rate else 0.1
        # price_tick = float(kline.price_tick) if kline.price_tick else 1.0
        # volume_multiple = float(kline.volume_multiple) if kline.volume_multiple else 1.0

        # ---- 从指标对象提取信号的辅助函数 ----
        def _extract_signals_from_ind(ind_obj):
            """从指标对象中提取 entries / exits，返回 (entries, exits)"""
            ls = getattr(ind_obj, 'long_signal', None)
            ss = getattr(ind_obj, 'short_signal', None)
            els = getattr(ind_obj, 'exitlong_signal', None)
            exs = getattr(ind_obj, 'exitshort_signal', None)

            if ss is None:
                ent = ls
            elif ls is None:
                ent = -ss
            else:
                ent = ls - ss

            ext = None
            if els is None:
                ext = exs
            elif exs is None:
                ext = -els
            else:
                ext = -els + exs
            if ext is None:
                ext = ent
            return ent, ext

        # ---- 策略构建模式（丰富结果展示）的共用函数 ----
        def _build_strategy_result(sig_entries, sig_exits, indicator=None):
            """策略构建模式：用策略框架跑完整回测，支持可视化/报告"""
            if indicator is None:
                indicator = self
            indicator_name = (getattr(indicator, 'name', None)
                              or getattr(indicator, '__name__', None)
                              or 'SignalIndicator')
            from ..strategy.strategy import Strategy
            from ..utils import Config

            class _SignalBacktestStrategy(Strategy):
                config = Config(value=init_cash)
                def __init__(self_):
                    if min_start_length is not None and min_start_length > 0:
                        self_.min_start_length = min_start_length
                    setattr(self_, "data", kline)
                    setattr(self_, indicator_name, indicator)

            sbr = _SignalBacktestStrategy()
            sbr.__class__.__name__ = f"{indicator_name}Strategy"
            # 1. 策略启动前准备
            sbr._prepare_before_strategy_start()
            sbr.bt_from_signals(
                entries=sig_entries,
                exits=sig_exits,
                size=size,
                size_type=size_type,
                commission=commission,
                com_type=com_type,
                slip_point=slip_point,
                prices=prices,
                sl_stop=sl_stop,
                tp_stop=tp_stop,
                stop_mode=stop_mode,
                sl_trail=sl_trail,
                sl_callback=sl_callback,
                tp_callback=tp_callback,
                sl_callback_args=sl_callback_args,
                tp_callback_args=tp_callback_args,
                max_hold_bars=max_hold_bars)
            sbr._execute_core_trading_loop()
            # 4. 整理绘图数据（后续可视化用）
            sbr._get_plot_datas()
            if isplot or isreport:
                sbr._print_account, sbr.richprint
            if isplot:
                from ..strategy.bokeh_plot import plot
                trade_signal: bool = kwargs.get("trade_signal", True)
                black_style: bool = kwargs.get("black_style", False)
                open_browser: bool = kwargs.get("open_browser", False)
                plot_width: int = kwargs.get("plot_width", None)
                plot_cwd = kwargs.get("plot_cwd", "")
                plot_name: str = kwargs.get("plot_name", "")
                save_plot: bool = kwargs.get("save_plot", False)
                plot([sbr,], trade_signal, black_style,
                     open_browser, plot_width, plot_cwd, plot_name, save_plot)
            if isreport:
                sbr._qs_reports(show=True)
            return sbr

        # ==================================================================
        # 分支：optimize=True / OptimizeConfig — 参数优化模式
        # ==================================================================
        if optimize:
            # ---- 解析优化配置参数 ----
            if isinstance(optimize, OptimizeConfig):
                # 从 OptimizeConfig 实例提取参数
                _opt_params = optimize.params  if optimize.params  else kwargs.pop('optimize_params', {})             # 待优化参数范围
                _opt_target = optimize.target               # 优化目标指标
                _opt_method = optimize.method               # 'grid' | 'optuna'
                _opt_weights_data = optimize.weights        # 优化权重
                _opt_config = optimize.config               # 优化器配置
                _opt_n_jobs = optimize.n_jobs               # 并行线程数
                _opt_output = optimize.output_dir           # CSV 输出目录
                # 权重优先取 OptimizeConfig，其次取 optimize_weights 参数
                _opt_weights = _opt_weights_data if _opt_weights_data is not None else optimize_weights
            else:
                # 旧版 kwargs 方式（向后兼容：optimize=True + kwargs）
                _opt_params = kwargs.pop('optimize_params', None)       # 待优化参数范围
                _opt_target = kwargs.pop('optimize_target', 'sharpe')   # 优化目标指标
                _opt_method = kwargs.pop('optimize_method', 'optuna')   # 'grid' | 'optuna'
                _opt_config = kwargs.pop('optimize_config', None)       # 优化配置(n_trials/n_jobs等)
                _opt_n_jobs = kwargs.pop('n_jobs', 1)                   # 并行线程数，-1=全核
                _opt_output = kwargs.pop('output_dir', '')              # CSV输出目录
                # 解析权重：优先取显式参数 optimize_weights，其次 kwargs 中的 optimize_weights
                _opt_weights = kwargs.pop('optimize_weights', None)
                if _opt_weights is None:
                    _opt_weights = optimize_weights

            # ---- 准备基础数据（转为numpy）----
            from . import IndSeries
            # 保留原始 kline 引用
            _base_kline = kline
            # 转换信号为二维数组
            if isinstance(entries, IndSeries):
                entries_arr = entries.values.reshape(-1, 1)
            else:
                entries_arr = np.atleast_2d(entries.values if hasattr(entries, 'values') else entries)
            if isinstance(exits, IndSeries):
                exits_arr = exits.values.reshape(-1, 1)
            else:
                exits_arr = np.atleast_2d(exits.values if hasattr(exits, 'values') else exits)

            _close_arr = np.atleast_2d(kline.close.values).T
            _margin_rate = float(kline.margin_rate) if kline.margin_rate else 0.1
            _price_tick = float(kline.price_tick) if kline.price_tick else 1.0
            _vol_mult = float(kline.volume_multiple) if kline.volume_multiple else 1.0
            _msl = self.min_start_length if hasattr(self, 'min_start_length') else min_start_length
            if _msl is None:
                _msl = 1

            _prices_arr = None
            if prices is not None:
                _prices_arr = prices.values.reshape(-1, 1) if isinstance(prices, IndSeries) else np.atleast_2d(prices)

            from ..cython_functions.backtrader_from_signals import from_signals
            # 提取回调函数 C 地址
            sl_cb_addr = 0
            tp_cb_addr = 0
            if sl_callback is not None:
                self._sl_callback_obj = sl_callback
                sl_cb_addr = sl_callback.address
            if tp_callback is not None:
                self._tp_callback_obj = tp_callback
                tp_cb_addr = tp_callback.address

            # ---- 单次回测核心函数 ----
            def _run_backtest(sig_entries, sig_exits, sig_prices=None):
                """给定信号数组，运行 from_signals 并返回 (equity_curve, total_fee)"""
                _e = np.atleast_1d(sig_entries).reshape(-1, 1)
                _x = np.atleast_1d(sig_exits).reshape(-1, 1)
                _p = sig_prices
                res = from_signals(
                    close=_close_arr, entries=_e, exits=_x,
                    size=size, size_type=size_type,
                    margin_rate=[_margin_rate],
                    price_tick=[_price_tick],
                    volume_multiple=[_vol_mult],
                    prices=_p, min_start_length=_msl,
                    init_cash=init_cash, commission=commission,
                    com_type=com_type, slip_point=slip_point,
                    sl_stop=sl_stop, tp_stop=tp_stop, stop_mode=stop_mode,
                    sl_trail=sl_trail,
                    sl_callback_addr=sl_cb_addr, tp_callback_addr=tp_cb_addr,
                    sl_callback_args=sl_callback_args, tp_callback_args=tp_callback_args,
                    max_hold_bars=max_hold_bars)
                # from_signals 返回 list[ndarray(shape=(n_bars,6))]
                eq_curve = res[0][:, 0]          # total_equity 每 bar
                total_fee_val = res[0][-1, 5]    # 累计手续费
                return eq_curve, total_fee_val


            # ---- 计算 quantstats 指标 ----
            def _calc_qs_metrics(equity_curve):
                """从权益曲线计算 quantstats 核心指标"""
                profits = pd.Series(equity_curve, index=_base_kline.datetime.values[:len(equity_curve)])
                returns = profits.pct_change().fillna(0.)
                returns.iloc[0] = 0.
                returns.index = pd.Index(_base_kline.datetime.values[:len(returns)])
                try:
                    from quantstats import stats as qs_stats
                except ImportError:
                    import quantstats as qs_stats
                m = {}
                try:
                    if len(returns.dropna()) > 1:
                        m['sharpe'] = float(qs_stats.sharpe(returns) or 0.)
                        m['sortino'] = float(qs_stats.sortino(returns) or 0.)
                        m['max_drawdown'] = float(qs_stats.max_drawdown(returns) or -1.)
                        m['calmar'] = float(qs_stats.calmar(returns) or 0.)
                        m['win_rate'] = float(qs_stats.win_rate(returns) or 0.)
                        m['profit_factor'] = float(qs_stats.profit_factor(returns) or 0.)
                        m['cagr'] = float(qs_stats.cagr(returns) or 0.)
                        m['value_at_risk'] = float(qs_stats.value_at_risk(returns) or 0.)
                        m['risk_return_ratio'] = float(qs_stats.risk_return_ratio(returns) or 0.)
                    else:
                        for k in ('sharpe','sortino','calmar','win_rate','profit_factor','cagr',
                                  'value_at_risk','risk_return_ratio'):
                            m[k] = 0.
                        m['max_drawdown'] = -1.
                except Exception:
                    for k in ('sharpe','sortino','calmar','win_rate','profit_factor','cagr',
                              'value_at_risk','risk_return_ratio'):
                        m[k] = 0.
                    m['max_drawdown'] = -1.
                return m, returns, profits

            # ---- 单次完整回测（含指标通过 params 重新运行）----
            # BtIndicator.__new__ 返回 IndFrame，需要用保存的原始指标类
            ind_cls = getattr(self, '_bt_indicator_cls', None)
            assert ind_cls is not None, "未设置 _bt_indicator_cls"
            _has_next = (hasattr(ind_cls, '_is_method_overridden') 
                         and ind_cls._is_method_overridden('next'))
            # 保存原始类级 params（若有）
            _orig_cls_params = getattr(ind_cls, 'params', Addict()).copy() if _has_next else None
            # 线程安全锁（BtIndicator.__new__ 会修改 cls.params）
            import threading
            _param_lock = threading.Lock()
            def _single_trial(params_dict: dict, trial_id: int = 0) -> dict:
                """对一组参数运行回测，返回指标字典和权益曲线"""
                p = params_dict.copy()
                eq, fee = None, 0.

                if _has_next and ind_cls is not BtIndicator:
                # if ind_cls is not BtIndicator:
                    # 通过 params 重新运行指标，生成新信号
                    # 使用锁保护 cls.params 修改（并行场景下线程安全）
                    # try:
                        with _param_lock:
                            ind_cls.params = Addict({**(_orig_cls_params or {}), **p})
                            #params = Addict({**(_orig_cls_params or {}), **p})
                            new_ind = ind_cls(_base_kline)
                            #print(ind_cls.params,new_ind.tail())
                        # 从新指标提取信号
                        ls = getattr(new_ind, 'long_signal', None)
                        ss = getattr(new_ind, 'short_signal', None)
                        els = getattr(new_ind, 'exitlong_signal', None)
                        exs = getattr(new_ind, 'exitshort_signal', None)
                        _e = np.zeros(len(_close_arr))
                        _x = np.zeros(len(_close_arr))
                        if ls is not None:
                            _e += ls.values if hasattr(ls, 'values') else ls
                        if ss is not None:
                            sv = ss.values if hasattr(ss, 'values') else ss
                            _e -= sv
                        if els is not None:
                            ev = els.values if hasattr(els, 'values') else els
                            _x -= ev
                        if exs is not None:
                            xv = exs.values if hasattr(exs, 'values') else exs
                            _x += xv
                        if _x is None or np.all(_x == 0):
                            _x = _e
                        # 检查是否有有效信号：entries 全为零则跳过回测
                        if np.all(_e == 0):
                            return {'params': p, 'trial': trial_id,
                                    'error': '无有效交易信号',
                                    'equity': np.full(len(_close_arr), init_cash),
                                    'total_fee': 0.}
                        eq, fee = _run_backtest(_e, _x, _prices_arr)
                    # except Exception as exc:
                    #     return {'params': p, 'trial': trial_id, 'error': str(exc),
                    #             'equity': eq, 'total_fee': 0.}
                else:
                    # 无 next() 或基类，使用当前信号（参数仅标记）
                    if np.all(entries_arr == 0):
                        return {'params': p, 'trial': trial_id,
                                'error': '无有效交易信号',
                                'equity': np.full(len(_close_arr), init_cash),
                                'total_fee': 0.}
                    eq, fee = _run_backtest(entries_arr, exits_arr, _prices_arr)

                metrics, returns, profits = _calc_qs_metrics(eq) if eq is not None else ({}, None, None)
                metrics['params'] = p
                metrics['trial'] = trial_id
                metrics['final_equity'] = float(eq[-1]) if eq is not None else 0.
                metrics['total_return_pct'] = (metrics['final_equity'] / init_cash - 1.) * 100.
                metrics['total_fee'] = float(fee) if fee is not None else 0.
                metrics['equity'] = eq
                metrics['returns'] = returns
                return metrics

            # ---- 无优化参数：仅跑一次 ----
            if not _opt_params:
                base_params = (getattr(self, 'params', Addict()).copy() 
                               if hasattr(self, 'params') else {})
                result_dict = _single_trial(base_params)
                if 'error' in result_dict:
                    raise RuntimeError(f"回测失败: {result_dict['error']}")
                # 返回轻量结果对象
                eq = result_dict.get('equity')
                sbr = SignalBacktestResult(
                    result_df=pd.DataFrame(result_dict),
                    profits=pd.Series(eq, index=_base_kline.datetime.values[:len(eq)]) if eq is not None else pd.Series(),
                    init_cash=init_cash,
                    total_fee=result_dict.get('total_fee', 0.),
                    total_profit=result_dict.get('final_equity', init_cash),
                    kline=_base_kline,
                    params=base_params)
                sbr.isplot = False
                if isplot:
                    sbr.plot()
                if isreport:
                    sbr.report()
                return sbr

            # ---- 解析待优化参数 ----
            if not isinstance(_opt_params, dict):
                raise ValueError("optimize_params 必须为 dict，如 {'length': (5,50,5)}")
            param_defs: dict = {}  # {param_name: {type:'int'|'float'|'cat', low, high, step/choices}}
            for k, v in _opt_params.items():
                if isinstance(v, (list, tuple)) and len(v) >= 2:
                    if isinstance(v, list) and any(isinstance(x, str) for x in v):
                        param_defs[k] = {'type': 'cat', 'choices': list(v)}
                    else:
                        low, high = float(v[0]), float(v[1])
                        step = float(v[2]) if len(v) > 2 else None
                        # 推断类型：所有数值均为整数（或等价于整数的浮点数）则推断为 int
                        is_int = all(isinstance(x, int) or (isinstance(x, float) and x == int(x))
                                     for x in v if isinstance(x, (int, float)))
                        if is_int and (step is None or int(step) == step):
                            param_defs[k] = {'type': 'int', 'low': int(low), 'high': int(high),
                                             'step': int(step) if step is not None else 1}
                        else:
                            param_defs[k] = {'type': 'float', 'low': low, 'high': high, 'step': step}
                else:
                    raise ValueError(f"optimize_params['{k}'] 格式错误，需要 (low,high) / (low,high,step) / [选项列表]")

            # ---- 解析优化目标和方向 ----
            if isinstance(_opt_target, str):
                targets = [_opt_target]
            elif isinstance(_opt_target, (list, tuple)):
                targets = list(_opt_target)
            else:
                raise ValueError("optimize_target 必须为字符串或列表")

            # ---- 解析权重（float | tuple[float]）----
            # >0：最大化目标，<0：最小化目标，绝对值越大权重越大
            # 不设置时默认最大化所有目标
            if _opt_weights is None:
                _opt_weights = tuple(1.0 for _ in targets)         # 默认：最大化，等权重
            elif isinstance(_opt_weights, (float, int)):
                if _opt_weights == 0:
                    raise ValueError("optimize_weights 不能为 0（无法判断优化方向）")
                _opt_weights = (float(_opt_weights),)
            elif isinstance(_opt_weights, (list, tuple)):
                _opt_weights = tuple(float(w) for w in _opt_weights)
            else:
                raise ValueError("optimize_weights 必须为 float | tuple[float]")
            if len(_opt_weights) != len(targets):
                raise ValueError(
                    f"optimize_weights 数量 ({len(_opt_weights)}) 与 targets 数量 ({len(targets)}) 不一致。"
                    f"targets={targets}"
                )
            # 方向：正权重 = 最大化，负权重 = 最小化
            directions = ["maximize" if w >= 0.0 else "minimize" for w in _opt_weights]

            # 计算加权分数（用于 grid 搜索排序 / 综合评分）
            def _weighted_score(r: dict) -> float:
                s = 0.0
                for t, w in zip(targets, _opt_weights):
                    val = r.get(t, 0.0)
                    s += val * w  # 正权重奖励高值，负权重奖励低值
                return s

            # ---- 优化配置默认值 ----
            _cfg = _opt_config if isinstance(_opt_config, dict) else {}
            n_trials = _cfg.get('n_trials', 100)
            n_jobs = _opt_n_jobs if _opt_n_jobs != 1 else _cfg.get('n_jobs', 1)

            # ---- 获取指标基类默认 params ----
            base_params = (getattr(self, 'params', Addict()).copy()
                           if hasattr(self, 'params') else {})

            # ================================================================
            # Grid Search（并行网格搜索）
            # ================================================================
            if _opt_method == 'grid':
                import itertools
                from concurrent.futures import ThreadPoolExecutor, as_completed

                # 生成参数组合
                param_ranges = []
                for k, pd_ in param_defs.items():
                    if pd_['type'] == 'cat':
                        param_ranges.append([(k, c) for c in pd_['choices']])
                    elif pd_['type'] == 'int':
                        vals = list(range(pd_['low'], pd_['high'] + 1, pd_.get('step', 1)))
                        param_ranges.append([(k, v) for v in vals])
                    else:  # float
                        step = pd_.get('step')
                        if step is not None:
                            vals = []
                            v = pd_['low']
                            while v <= pd_['high'] + 1e-10:
                                vals.append(round(v, 6))
                                v += step
                        else:
                            # 均匀 20 点
                            vals = np.linspace(pd_['low'], pd_['high'], 20).tolist()
                        param_ranges.append([(k, round(v, 6)) for v in vals])

                param_combos = []
                for combo in itertools.product(*param_ranges):
                    d = dict(combo)
                    # 合并 base_params
                    full = {**base_params, **d}
                    param_combos.append(full)

                n_combos = len(param_combos)
                weights_info = ", ".join(f"{t}({'max' if w>=0 else 'min'},w={abs(w):.1f})" 
                                          for t, w in zip(targets, _opt_weights))
                print(f"网格搜索：{n_combos} 组参数组合，目标={targets}，权重=[{weights_info}]，并行进程={n_jobs}")

                all_results = []
                if n_jobs in (-1, 'max'):
                    import os as _os_
                    n_jobs = min(_os_.cpu_count() or 4, n_combos)
                if n_jobs <= 1 or n_combos <= 1:
                    for i, p in enumerate(param_combos):
                        all_results.append(_single_trial(p, i))
                else:
                    with ThreadPoolExecutor(max_workers=n_jobs) as executor:
                        futures = {executor.submit(_single_trial, p, i): i for i, p in enumerate(param_combos)}
                        for fut in as_completed(futures):
                            try:
                                all_results.append(fut.result())
                            except Exception as exc:
                                idx = futures[fut]
                                all_results.append({'params': param_combos[idx], 'trial': idx,
                                                     'error': str(exc)})

                # 过滤错误结果并排序
                valid_results = [r for r in all_results if 'error' not in r]
                if not valid_results:
                    raise RuntimeError("所有回测试验均失败")

                # 按加权分数排序（权重 >0 奖励大值，<0 奖励小值）
                valid_results.sort(key=_weighted_score, reverse=True)

                best = valid_results[0]
                best_params = {k: v for k, v in best['params'].items() if k in _opt_params}

                print(f"\n{'='*60}")
                print(f"网格搜索最优参数 (n={len(valid_results)}/{len(all_results)})，加权得分={_weighted_score(best):.4f}:")
                for k, v in best_params.items():
                    print(f"  {k}: {v}")
                for t, w in zip(targets, _opt_weights):
                    print(f"  {t}: {best.get(t, 'N/A')}  (权重={'最大化' if w>=0 else '最小化'}, |w|={abs(w):.1f})")
                print(f"{'='*60}")

                # 输出 CSV
                if _opt_output:
                    import os
                    os.makedirs(_opt_output, exist_ok=True)
                    df_rows = []
                    for r in valid_results[:50]:
                        row = {k: r[k] for k in targets}
                        row.update({k: v for k, v in r['params'].items() if k in _opt_params})
                        row['_weighted_score'] = _weighted_score(r)
                        df_rows.append(row)
                    df = pd.DataFrame(df_rows)
                    csv_path = os.path.join(_opt_output, f"grid_search_{targets[0]}.csv")
                    df.to_csv(csv_path, index=False)
                    print(f"结果已保存至: {csv_path}")
                    print(df.head(10).to_string())

                # 用最优参数重新运行指标，获取最优信号
                if _has_next and ind_cls is not BtIndicator:
                    with _param_lock:
                        ind_cls.params = Addict({**(_orig_cls_params or {}), **best_params})
                    best_ind = ind_cls(_base_kline)
                else:
                    best_ind = self
                post_entries, post_exits = _extract_signals_from_ind(best_ind)

                # 策略构建模式运行，得到丰富的结果展示
                if _has_valid_signals(post_entries):
                    t = _build_strategy_result(post_entries, post_exits, best_ind)
                else:
                    print(f"[警告] 最优参数未产生有效交易信号，跳过策略构建与绘图。最优参数={best_params}")
                    t = None
                return OptimizationResult(best_params=best_params,
                                          best_score=_weighted_score(best),
                                          best_metrics=best, all_results=valid_results,
                                          method='grid', target=targets,
                                          weights=_opt_weights, result=t)

            # ================================================================
            # Optuna 贝叶斯优化（支持单/多目标）
            # ================================================================
            elif _opt_method == 'optuna':
                import optuna

                # 配置 Optuna 采样器和剪枝器
                study_kwargs: dict = {}
                sampler_name = _cfg.get('sampler', 'NSGAIISampler')
                pruner_name = _cfg.get('pruner', 'HyperbandPruner')

                if len(targets) == 1:
                    study_kwargs['direction'] = directions[0]
                else:
                    study_kwargs['directions'] = directions

                # 采样器
                if sampler_name and hasattr(optuna.samplers, sampler_name):
                    study_kwargs['sampler'] = getattr(optuna.samplers, sampler_name)()
                # 剪枝器
                if pruner_name and hasattr(optuna.pruners, pruner_name):
                    study_kwargs['pruner'] = getattr(optuna.pruners, pruner_name)()

                # 日志控制
                if not _cfg.get('verbose', True):
                    optuna.logging.set_verbosity(optuna.logging.WARNING)

                study = optuna.create_study(**study_kwargs)

                # 定义采样函数
                def _get_params(trial: optuna.Trial) -> Addict:
                    p = Addict(base_params.copy())
                    for k, pd_ in param_defs.items():
                        if pd_['type'] == 'cat':
                            p[k] = trial.suggest_categorical(k, pd_['choices'])
                        elif pd_['type'] == 'int':
                            step = pd_.get('step', 1)
                            p[k] = trial.suggest_int(k, pd_['low'], pd_['high'], step=step)
                        else:  # float
                            step = pd_.get('step')
                            if step is not None:
                                p[k] = trial.suggest_float(k, pd_['low'], pd_['high'], step=step)
                            else:
                                p[k] = trial.suggest_float(k, pd_['low'], pd_['high'])
                    return p

                # 定义目标函数
                def _objective(trial: optuna.Trial):
                    p = _get_params(trial)
                    ret = _single_trial(dict(p), trial.number)
                    if 'error' in ret:
                        if len(targets) == 1:
                            return -float('inf') if directions[0] == 'maximize' else float('inf')
                        else:
                            return tuple(-float('inf') if d == 'maximize' else float('inf') for d in directions)
                    vals = [ret.get(t, 0.) for t in targets]
                    if len(targets) == 1:
                        return vals[0]
                    return tuple(vals)

                weights_info = ", ".join(f"{t}({'max' if w>=0 else 'min'},w={abs(w):.1f})"
                                          for t, w in zip(targets, _opt_weights))
                print(f"Optuna 优化：n_trials={n_trials}, targets={targets}, 权重=[{weights_info}], n_jobs={n_jobs}")
                study.optimize(_objective, n_trials=n_trials, n_jobs=n_jobs,
                               show_progress_bar=_cfg.get('show_progress_bar', True))

                # 提取最优结果
                if len(targets) == 1:
                    best_trial = study.best_trial
                else:
                    trials_sorted = sorted(study.best_trials, key=lambda t: t.values, reverse=True)
                    best_trial = trials_sorted[-1] if trials_sorted else None

                if best_trial is None:
                    raise RuntimeError("Optuna 未找到有效试验")

                best_params = best_trial.params
                print(f"\n{'='*60}")
                print(f"Optuna 最优参数 (n_trials={len(study.trials)}):")
                for k, v in best_params.items():
                    if k in _opt_params:
                        print(f"  {k}: {v}")
                if len(targets) == 1:
                    print(f"  {targets[0]}: {best_trial.value}  (权重={'最大化' if _opt_weights[0]>=0 else '最小化'}, |w|={abs(_opt_weights[0]):.1f})")
                else:
                    value_dict = dict(zip(targets, best_trial.values))
                    for t, w in zip(targets, _opt_weights):
                        print(f"  {t}: {value_dict.get(t, 'N/A')}  (权重={'最大化' if w>=0 else '最小化'}, |w|={abs(w):.1f})")
                print(f"{'='*60}")

                # 保存 CSV
                if _opt_output:
                    import os
                    os.makedirs(_opt_output, exist_ok=True)
                    df = study.trials_dataframe(attrs=('number', 'value', 'params'))
                    value_cols = [c for c in df.columns if 'value' in c]
                    df.sort_values(by=value_cols, ascending=False, inplace=True, ignore_index=True)
                    csv_path = os.path.join(_opt_output, f"optuna_{targets[0]}.csv")
                    df.to_csv(csv_path, index=False)
                    print(f"结果已保存至: {csv_path}")
                    print(df.head(10).to_string())

                # 用最优参数重新运行指标，获取最优信号
                best_metrics = _single_trial(dict(best_params))
                if _has_next and ind_cls is not BtIndicator:
                    with _param_lock:
                        ind_cls.params = Addict({**(_orig_cls_params or {}), **best_params})
                    best_ind = ind_cls(_base_kline)
                else:
                    best_ind = self
                post_entries, post_exits = _extract_signals_from_ind(best_ind)

                # 策略构建模式运行，得到丰富的结果展示
                if _has_valid_signals(post_entries):
                    t = _build_strategy_result(post_entries, post_exits, best_ind)
                else:
                    print(f"[警告] 最优参数未产生有效交易信号，跳过策略构建与绘图。最优参数={best_params}")
                    t = None
                return OptimizationResult(best_params=best_params,
                                          best_score=_weighted_score(best_metrics),
                                          best_metrics=best_metrics,
                                          study=study, method='optuna',
                                          target=targets, weights=_opt_weights, result=t)

            else:
                raise ValueError(f"不支持的优化方法: {_opt_method}，可选 'grid' 或 'optuna'")
        else:#策略构建模式，更丰富的结果展示
            return _build_strategy_result(entries, exits)

    def pair_backtest(self,
                     kline_a: KLine = None,
                     kline_b: KLine = None,
                     size: float = 1.0,
                     size_type: SizeType | None = None,
                     commission: list[float] | tuple[float, float] | None = None,
                      com_type: int | CommissionType | None = None,
                      slip_point: float | None = None,
                      margin_rate: list[float] | tuple[float, float] | None = None,
                      price_tick: list[float] | tuple[float, float] | None = None,
                      volume_multiple: list[float] | tuple[float, float] | None = None,
                      leg_size_ratio: float | tuple[float, float] = 1.0,
                      prices: np.ndarray | IndSeries | None = None,
                      init_cash: float = 1000000.0,
                      min_start_length: int | None = None,
                      isplot: bool = True,
                      isreport: bool = True,
                      sl_stop: float = 0.0,
                      tp_stop: float = 0.0,
                      stop_mode: Literal[0, 1, 2] = 0,
                      sl_trail: Literal[0, 1] = 0,
                      max_hold_bars: int = 0,
                      optimize: bool | OptimizeConfig = False,
                      optimize_weights: float | tuple[float, ...] | None = None,
                      **kwargs) -> Strategy | SignalBacktestResult | OptimizationResult | None:
        """
        ## 配对交易快速Cython回测（专门针对价差/配对策略）

        基于 IndFrame 内置的交易信号（``long_signal`` / ``short_signal`` / ``exitlong_signal`` / ``exitshort_signal``），
        通过 Cython 加速的 ``pair_from_signals`` 引擎执行配对交易回测。

        ### 配对交易逻辑：
        - **做多价差**（``long_signal``）：买 legs[0]（多头），卖 legs[1]（空头）
        - **做空价差**（``short_signal``）：卖 legs[0]（空头），买 legs[1]（多头）
        - **级联平仓**：任意一条腿离场（止损/止盈/信号）→ 另一条腿同步强制平仓
        - **原子入场**：两条腿联合资金验证，要么同时入场，要么都不入场

        ### Args:
        | 参数 | 类型 | 默认值 | 说明 |
        |---|---|---|---|
        | ``kline_a`` | ``KLine | None`` | ``None`` | 腿A的 KLine（用于提取费用/保证金参数，不提供则用默认值） |
        | ``kline_b`` | ``KLine | None`` | ``None`` | 腿B的 KLine |
        | ``size`` | ``float`` | ``1.0`` | 交易规模 |
        | ``size_type`` | ``SizeType | None`` | ``None`` | 规模类型 |
        | ``commission`` | ``list[float] | None`` | ``None`` | [leg_a, leg_b] 手续费 |
        | ``com_type`` | ``int | CommissionType | None`` | ``None`` | 手续费类型 |
        | ``slip_point`` | ``float | None`` | ``None`` | 滑点 |
        | ``margin_rate`` | ``list[float] | None`` | ``None`` | [leg_a, leg_b] 保证金比例 |
        | ``price_tick`` | ``list[float] | None`` | ``None`` | [leg_a, leg_b] 最小变动单位 |
        | ``volume_multiple`` | ``list[float] | None`` | ``None`` | [leg_a, leg_b] 合约乘数 |
        | ``leg_size_ratio`` | ``float | tuple[float,float]`` | ``1.0`` | A:B 手数比例 |
        | ``prices`` | ``np.ndarray | None`` | ``None`` | (n,2) 成交价 |
        | ``optimize`` | ``bool | OptimizeConfig`` | ``False`` | 参数优化模式 |

        ### Example:
            >>> close1 = LocalDatas.v2609_60.kline.close.values
            >>> close2 = LocalDatas.pp2609_60.kline.close.values
            >>> df = IndFrame(dict(v_close=close2, pp_close=close1))
            >>> kf = KalmanFilter(df)
            >>> # 设置信号
            >>> kf = KalmanFilter(df)
            >>> result = df.pair_backtest(
            ...     kline_a=LocalDatas.v2609_60.kline,
            ...     kline_b=LocalDatas.pp2609_60.kline,
            ...     optimize=True,
            ...     optimize_params={'OPEN_H': (1.5, 3.0, 0.1), 'OPEN_L': (-3.0, -1.5, 0.1)},
            ... )
        """
        from ..other import SizeType, CommissionType

        # ---- 1. 解析费用/保证金参数（优先用户传入，其次 KLine，最后默认值）----
        def _get_dual_param(kline_a, kline_b, user_val, attr, default_a, default_b):
            """解析两条腿的参数，返回 [val_a, val_b]"""
            if user_val is not None:
                if isinstance(user_val, (int, float)):
                    return [float(user_val), float(user_val)]
                return [float(user_val[0]), float(user_val[1])]
            va = default_a
            vb = default_b
            if kline_a is not None:
                va = float(getattr(kline_a, attr, default_a) or default_a)
            if kline_b is not None:
                vb = float(getattr(kline_b, attr, default_b) or default_b)
            return [va, vb]

        if size_type is None:
            size_type = SizeType.Amount

        _margin_rate = _get_dual_param(kline_a, kline_b, margin_rate, 'margin_rate', 0.1, 0.1)
        _price_tick = _get_dual_param(kline_a, kline_b, price_tick, 'price_tick', 1.0, 1.0)
        _vol_mult = _get_dual_param(kline_a, kline_b, volume_multiple, 'volume_multiple', 5.0, 5.0)

        if commission is None:
            _commission = _get_dual_param(kline_a, kline_b, None, 'commission', 1.0, 1.0)
        else:
            _commission = _get_dual_param(kline_a, kline_b, commission, '', 0.0, 0.0)

        if com_type is None:
            ct = None
            if kline_a is not None:
                ct = kline_a.commission_type
            elif kline_b is not None:
                ct = kline_b.commission_type
            com_type = int(ct) if ct is not None else int(CommissionType.Fixed)

        if slip_point is None:
            sp = None
            if kline_a is not None:
                sp = kline_a.slip_point
            elif kline_b is not None:
                sp = kline_b.slip_point
            slip_point = float(sp) if sp is not None else 0.0

        if min_start_length is None:
            msl = None
            if kline_a is not None:
                msl = getattr(kline_a, 'min_start_length', None)
            elif kline_b is not None:
                msl = getattr(kline_b, 'min_start_length', None)
            min_start_length = int(msl) if msl is not None else 1

        # ---- 2. 提取信号数据 ----
        long_sig = self.long_signal
        short_sig = self.short_signal
        exitlong_sig = self.exitlong_signal
        exitshort_sig = self.exitshort_signal

        if long_sig is None and short_sig is None:
            raise ValueError(
                "请先设置 long_signal 和/或 short_signal。\n"
                "示例: kf.long_signal = zscores < -2.0"
            )

        # entries 和 exits 信号构造（与 signal_backtest 一致）
        if short_sig is None:
            entries = long_sig
        elif long_sig is None:
            entries = -short_sig
        else:
            entries = long_sig - short_sig

        exits = None
        if exitlong_sig is None:
            exits = exitshort_sig
        elif exitshort_sig is None:
            exits = -exitlong_sig
        else:
            exits = -exitlong_sig + exitshort_sig
            # ===== 配对交易特殊处理 =====
            # 配对交易中 exitlong/exitshort 通常定义为相同条件（如 zscore 回归），
            # 此时 -exitlong + exitshort = 0 会将所有出场信号归零！
            # 回退到使用 exitlong_sig 的负值作为通用平仓信号：
            #   多头腿(dir=+1)直接匹配 exits<0 触发平仓 → 级联平空头腿
            #   空头腿(dir=-1)不直接匹配，但对侧多头腿先平 → 级联平仓
            try:
                _ev = exits.values if hasattr(exits, 'values') else np.asarray(exits)
                if exitlong_sig is not None and np.all(_ev == 0):
                    exits = -exitlong_sig
            except Exception:
                pass
        if exits is None:
            exits = entries

        # ---- 信号有效性检查 ----
        def _has_valid(arr):
            if arr is None:
                return False
            vals = arr.values if hasattr(arr, 'values') else np.asarray(arr)
            return np.any(vals != 0)

        if not _has_valid(entries):
            raise ValueError("指标未产生任何有效的交易信号（entries 全为零）。")

        # ---- 提取 npy 数组 ----
        from . import IndSeries
        if isinstance(entries, IndSeries):
            entries_arr = entries.values
        else:
            entries_arr = entries.values if hasattr(entries, 'values') else np.asarray(entries)
        entries_arr = np.atleast_1d(entries_arr)

        if isinstance(exits, IndSeries):
            exits_arr = exits.values
        else:
            exits_arr = exits.values if hasattr(exits, 'values') else np.asarray(exits)
        exits_arr = np.atleast_1d(exits_arr)

        # ---- 获取价格数据 ----
        # prices 参数优先（传入的真实价格，如 df），否则 fallback 到 self 前两列
        _prices_arr = None
        if prices is not None:
            if isinstance(prices, IndSeries):
                _prices_arr = prices.values
            else:
                _prices_arr = np.asarray(prices, dtype=np.float64)
            if _prices_arr.ndim == 1:
                _prices_arr = np.column_stack([_prices_arr, _prices_arr])
            close_arr = _prices_arr.astype(np.float64)
        elif self.shape[1] >= 2:
            close_arr = self.values[:, :2].astype(np.float64)
        else:
            raise ValueError(f"IndFrame 需要至少 2 列价格数据，当前列数: {self.shape[1]}")

        # 时间索引（优先用 kline_a 的）
        _datetime_idx = None
        if kline_a is not None and hasattr(kline_a, 'datetime'):
            _datetime_idx = kline_a.datetime.values

        # ---- 3. 调用 Cython 回测 ----
        from ..cython_functions.backtrader_pair_from_signals import pair_from_signals

        def _run_pair_backtest(ent_arr, ext_arr, prices_arr=None):
            res = pair_from_signals(
                close=close_arr,
                entries=ent_arr,
                exits=ext_arr,
                size=size,
                size_type=int(size_type),
                margin_rate=_margin_rate,
                price_tick=_price_tick,
                volume_multiple=_vol_mult,
                prices=prices_arr,
                min_start_length=min_start_length,
                init_cash=init_cash,
                commission=_commission,
                com_type=int(com_type),
                slip_point=float(slip_point),
                sl_stop=sl_stop,
                tp_stop=tp_stop,
                stop_mode=stop_mode,
                sl_trail=sl_trail,
                max_hold_bars=max_hold_bars,
                leg_size_ratio=leg_size_ratio,
            )
            return res

        # ---- 4. 策略构建模式（非优化）----
        def _build_pair_strategy_result(sig_entries, sig_exits, indicator=None):
            if indicator is None:
                indicator = self
            indicator_name = (getattr(indicator, 'name', None)
                              or getattr(indicator, '__name__', None)
                              or 'PairIndicator')

            from ..strategy.strategy import Strategy
            from ..utils import Config

            class _PairBacktestStrategy(Strategy):
                config = Config(value=init_cash)

                def __init__(self_):
                    if min_start_length is not None and min_start_length > 0:
                        self_.min_start_length = min_start_length
                    if kline_a is not None:
                        setattr(self_, "kline_a", kline_a)
                    if kline_b is not None:
                        setattr(self_, "kline_b", kline_b)
                    setattr(self_, indicator_name, indicator)

                def bt_pair_from_signals(self_, entries, exits, prices=None,
                                         size=1.0, size_type=SizeType.Amount,
                                         commission=None, com_type=1,
                                         slip_point=0.0,
                                         sl_stop=0.0, tp_stop=0.0,
                                         stop_mode=0, sl_trail=0,
                                         max_hold_bars=0,
                                         leg_size_ratio=1.0,
                                         min_start_length=1,
                                         init_cash=1000000.0):
                    """配对交易信号回测（策略内部调用 Cython 引擎）。"""
                    from functools import partial
                    from ..cython_functions.backtrader_pair_from_signals import pair_from_signals

                    close = np.column_stack([
                        self_.kline_a.close.values,
                        self_.kline_b.close.values,
                    ])
                    entries_arr = (entries.values
                                   if hasattr(entries, 'values')
                                   else np.asarray(entries))
                    exits_arr = (exits.values
                                 if hasattr(exits, 'values')
                                 else np.asarray(exits))
                    prices_arr = prices if prices is not None else close

                    if commission is None:
                        commission = [
                            float(getattr(self_.kline_a, 'commission', 1.0) or 1.0),
                            float(getattr(self_.kline_b, 'commission', 1.0) or 1.0),
                        ]

                    margin_rate = [
                        float(getattr(self_.kline_a, 'margin_rate', 0.1) or 0.1),
                        float(getattr(self_.kline_b, 'margin_rate', 0.1) or 0.1),
                    ]
                    price_tick = [
                        float(getattr(self_.kline_a, 'price_tick', 1.0) or 1.0),
                        float(getattr(self_.kline_b, 'price_tick', 1.0) or 1.0),
                    ]
                    volume_multiple = [
                        float(getattr(self_.kline_a, 'volume_multiple', 5.0) or 5.0),
                        float(getattr(self_.kline_b, 'volume_multiple', 5.0) or 5.0),
                    ]

                    self_._isbacktrader_form_signals = True
                    self_._bt_from_signals_func = partial(
                        pair_from_signals,
                        close=close,
                        entries=entries_arr,
                        exits=exits_arr,
                        size=size,
                        size_type=int(size_type),
                        margin_rate=margin_rate,
                        price_tick=price_tick,
                        volume_multiple=volume_multiple,
                        prices=prices_arr,
                        min_start_length=min_start_length,
                        init_cash=init_cash,
                        commission=commission,
                        com_type=int(com_type),
                        slip_point=float(slip_point),
                        sl_stop=sl_stop,
                        tp_stop=tp_stop,
                        stop_mode=stop_mode,
                        sl_trail=sl_trail,
                        max_hold_bars=max_hold_bars,
                        leg_size_ratio=leg_size_ratio,
                    )
                    return self_._bt_from_signals_func

            sbr = _PairBacktestStrategy()
            sbr.__class__.__name__ = f"{indicator_name}PairStrategy"

            # ===== 标准策略回测流程（与 signal_backtest 的 _build_strategy_result 一致） =====
            sbr._prepare_before_strategy_start()
            sbr.bt_pair_from_signals(
                entries=sig_entries,
                exits=sig_exits,
                prices=_prices_arr,
                size=size,
                size_type=size_type,
                commission=commission,
                com_type=com_type,
                slip_point=slip_point,
                sl_stop=sl_stop,
                tp_stop=tp_stop,
                stop_mode=stop_mode,
                sl_trail=sl_trail,
                max_hold_bars=max_hold_bars,
                leg_size_ratio=leg_size_ratio,
                min_start_length=min_start_length,
                init_cash=init_cash,
            )
            sbr._execute_core_trading_loop()
            sbr._get_plot_datas()

            # 提取关键指标供 CLI / 外部使用
            results = sbr.get_results()
            eq_curve = results[0]["total_profit"].values
            sbr._final_equity = float(eq_curve[-1])
            sbr._total_fee = float(results[0]["total_fee"].iloc[-1])
            sbr._cum_profit = float(results[0]["cum_profits"].iloc[-1])
            if _datetime_idx is not None:
                sbr._equity_curve = pd.Series(
                    eq_curve, index=_datetime_idx[:len(eq_curve)])
            else:
                sbr._equity_curve = pd.Series(eq_curve)

            if isplot:
                try:
                    from ..strategy.bokeh_plot import plot as bokeh_plot
                    from bokeh.resources import INLINE
                    from bokeh.embed import file_html
                    tabs = bokeh_plot([sbr], True, False, False, None, '', '', False)
                    html_path = "pair_backtest_plot.html"
                    html = file_html(tabs, INLINE, "Pair Backtest")
                    with open(html_path, 'w', encoding='utf-8') as f:
                        f.write(html)
                    print(f"[plot] minibt 图表已保存: {os.path.abspath(html_path)}")
                except Exception as e:
                    import traceback
                    print(f"[plot] bokeh 图表显示失败: {e}")
                    traceback.print_exc()
                    _show_equity_chart(sbr)
            if isreport:
                try:
                    sbr._qs_reports(show=True)
                except Exception:
                    pass
            return sbr

        # ==================================================================
        # 分支：optimize=True / OptimizeConfig — 参数优化模式
        # ==================================================================
        if optimize:
            if isinstance(optimize, OptimizeConfig):
                _opt_params = optimize.params if optimize.params else kwargs.pop('optimize_params', {})
                _opt_target = optimize.target
                _opt_method = optimize.method
                _opt_weights_data = optimize.weights
                _opt_config = optimize.config
                _opt_n_jobs = optimize.n_jobs if optimize.n_jobs != 1 else kwargs.pop('n_jobs', 1)
                _opt_output = optimize.output_dir
                _opt_weights = _opt_weights_data if _opt_weights_data is not None else optimize_weights
            else:
                _opt_params = kwargs.pop('optimize_params', None)
                _opt_target = kwargs.pop('optimize_target', 'sharpe')
                _opt_method = kwargs.pop('optimize_method', 'optuna')
                _opt_config = kwargs.pop('optimize_config', None)
                _opt_n_jobs = kwargs.pop('n_jobs', 1)
                _opt_output = kwargs.pop('output_dir', '')
                _opt_weights = kwargs.pop('optimize_weights', None)
                if _opt_weights is None:
                    _opt_weights = optimize_weights

            # ---- 指标类信息 ----
            ind_cls = getattr(self, '_bt_indicator_cls', None)
            if ind_cls is None:
                raise ValueError("pair_backtest 需要指标由 BtIndicator 创建（需设置 _bt_indicator_cls）。")
            _has_next = (hasattr(ind_cls, '_is_method_overridden')
                         and ind_cls._is_method_overridden('next'))
            _orig_cls_params = getattr(ind_cls, 'params', Addict()).copy() if _has_next else None
            import threading
            _param_lock = threading.Lock()

            # 构建配对价格数据源（供优化时重新创建指标用）
            # self 是 pair_backtest 的接收者（即指标结果 IndFrame），它可能不包含 l_close/pp_close。
            # 因此从 close_arr（shape=n×2）重建一个包含两腿价格的 IndFrame。
            _pair_data = IndFrame(dict(
                l_close=close_arr[:, 0],
                pp_close=close_arr[:, 1],
            ))

            def _single_pair_trial(params_dict: dict, trial_id: int = 0) -> dict:
                p = params_dict.copy()
                eq, fee = None, 0.
                _e_arr = entries_arr
                _x_arr = exits_arr

                if _has_next and ind_cls is not BtIndicator:
                    with _param_lock:
                        ind_cls.params = Addict({**(_orig_cls_params or {}), **p})
                        new_ind = ind_cls(_pair_data)
                    ls = getattr(new_ind, 'long_signal', None)
                    ss = getattr(new_ind, 'short_signal', None)
                    els = getattr(new_ind, 'exitlong_signal', None)
                    exs = getattr(new_ind, 'exitshort_signal', None)
                    _e = np.zeros(len(close_arr))
                    _x = np.zeros(len(close_arr))
                    if ls is not None:
                        _e += ls.values if hasattr(ls, 'values') else ls
                    if ss is not None:
                        _e -= ss.values if hasattr(ss, 'values') else ss
                    if els is not None:
                        _x -= els.values if hasattr(els, 'values') else els
                    if exs is not None:
                        _x += exs.values if hasattr(exs, 'values') else exs
                    if np.all(_x == 0):
                        _x = _e
                    if np.all(_e == 0):
                        return {'params': p, 'trial': trial_id,
                                'error': '无有效交易信号',
                                'equity': np.full(len(close_arr), init_cash),
                                'total_fee': 0.}
                    _e_arr = _e
                    _x_arr = _x

                if np.all(_e_arr == 0):
                    return {'params': p, 'trial': trial_id,
                            'error': '无有效交易信号',
                            'equity': np.full(len(close_arr), init_cash),
                            'total_fee': 0.}

                res = _run_pair_backtest(_e_arr, _x_arr, _prices_arr)
                eq = res[0][:, 0]
                fee = float(res[0][-1, 5])   # res[0]和res[1]的total_fee相同，取一边即可

                # 计算指标
                if _datetime_idx is not None:
                    profits = pd.Series(eq, index=_datetime_idx[:len(eq)])
                else:
                    profits = pd.Series(eq)
                returns = profits.pct_change().fillna(0.)
                returns.iloc[0] = 0.
                try:
                    from quantstats import stats as qs_stats
                except ImportError:
                    import quantstats as qs_stats
                m = {}
                try:
                    if len(returns.dropna()) > 1:
                        m['sharpe'] = float(qs_stats.sharpe(returns) or 0.)
                        m['sortino'] = float(qs_stats.sortino(returns) or 0.)
                        m['max_drawdown'] = float(qs_stats.max_drawdown(returns) or -1.)
                        m['calmar'] = float(qs_stats.calmar(returns) or 0.)
                        m['win_rate'] = float(qs_stats.win_rate(returns) or 0.)
                        m['profit_factor'] = float(qs_stats.profit_factor(returns) or 0.)
                        m['cagr'] = float(qs_stats.cagr(returns) or 0.)
                    else:
                        for k in ('sharpe','sortino','calmar','win_rate','profit_factor','cagr'):
                            m[k] = 0.
                        m['max_drawdown'] = -1.
                except Exception:
                    for k in ('sharpe','sortino','calmar','win_rate','profit_factor','cagr'):
                        m[k] = 0.
                    m['max_drawdown'] = -1.

                m['params'] = p
                m['trial'] = trial_id
                m['final_equity'] = float(eq[-1]) if eq is not None else 0.
                m['total_return_pct'] = (m['final_equity'] / init_cash - 1.) * 100.
                m['total_fee'] = float(fee) if fee is not None else 0.
                m['equity'] = eq
                m['returns'] = returns
                return m

            # ---- 解析优化参数 ----
            if not _opt_params:
                base_params = (getattr(self, 'params', Addict()).copy()
                               if hasattr(self, 'params') else {})
                result_dict = _single_pair_trial(base_params)
                if 'error' in result_dict:
                    raise RuntimeError(f"回测失败: {result_dict['error']}")
                eq = result_dict.get('equity')
                sbr = SignalBacktestResult(
                    result_df=pd.DataFrame(result_dict),
                    profits=pd.Series(eq, index=_datetime_idx[:len(eq)] if _datetime_idx is not None else None) if eq is not None else pd.Series(),
                    init_cash=init_cash,
                    total_fee=result_dict.get('total_fee', 0.),
                    total_profit=result_dict.get('final_equity', init_cash),
                    kline=kline_a,
                    params=base_params)
                if isplot:
                    sbr.plot()
                if isreport:
                    sbr.report()
                return sbr

            if not isinstance(_opt_params, dict):
                raise ValueError("optimize_params 必须为 dict")

            # 解析参数定义
            param_defs = {}
            for k, v in _opt_params.items():
                if isinstance(v, (list, tuple)) and len(v) >= 2:
                    if isinstance(v, list) and any(isinstance(x, str) for x in v):
                        param_defs[k] = {'type': 'cat', 'choices': list(v)}
                    else:
                        low, high = float(v[0]), float(v[1])
                        step = float(v[2]) if len(v) > 2 else None
                        is_int = all(isinstance(x, int) or (isinstance(x, float) and x == int(x))
                                     for x in v if isinstance(x, (int, float)))
                        if is_int and (step is None or int(step) == step):
                            param_defs[k] = {'type': 'int', 'low': int(low), 'high': int(high),
                                             'step': int(step) if step is not None else 1}
                        else:
                            param_defs[k] = {'type': 'float', 'low': low, 'high': high, 'step': step}
                else:
                    raise ValueError(f"optimize_params['{k}'] 格式错误")

            # 解析目标
            if isinstance(_opt_target, str):
                targets = [_opt_target]
            elif isinstance(_opt_target, (list, tuple)):
                targets = list(_opt_target)
            else:
                raise ValueError("optimize_target 必须为字符串或列表")

            # 解析权重
            if _opt_weights is None:
                _opt_weights = tuple(1.0 for _ in targets)
            elif isinstance(_opt_weights, (float, int)):
                if _opt_weights == 0:
                    raise ValueError("optimize_weights 不能为 0")
                _opt_weights = (float(_opt_weights),)
            elif isinstance(_opt_weights, (list, tuple)):
                _opt_weights = tuple(float(w) for w in _opt_weights)
            if len(_opt_weights) != len(targets):
                raise ValueError(f"optimize_weights 数量 ({len(_opt_weights)}) 与 targets ({len(targets)}) 不一致")

            directions = ["maximize" if w >= 0.0 else "minimize" for w in _opt_weights]

            def _weighted_score(r):
                s = 0.0
                for t, w in zip(targets, _opt_weights):
                    s += r.get(t, 0.0) * w
                return s

            _cfg = _opt_config if isinstance(_opt_config, dict) else {}
            n_trials = _cfg.get('n_trials', 100)
            n_jobs = _opt_n_jobs if _opt_n_jobs != 1 else _cfg.get('n_jobs', 1)

            # ---- Grid Search ----
            if _opt_method == 'grid':
                import itertools
                from concurrent.futures import ThreadPoolExecutor, as_completed

                param_grid = {}
                for k, d in param_defs.items():
                    if d['type'] == 'cat':
                        param_grid[k] = d['choices']
                    elif d['type'] == 'int':
                        param_grid[k] = list(range(d['low'], d['high'] + 1, d.get('step', 1)))
                    else:
                        if d.get('step'):
                            vals = [d['low']]
                            while vals[-1] + d['step'] <= d['high'] + 1e-9:
                                vals.append(round(vals[-1] + d['step'], 10))
                            param_grid[k] = vals
                        else:
                            param_grid[k] = [d['low'], d['high']]

                keys = list(param_grid.keys())
                combos = [dict(zip(keys, v)) for v in itertools.product(*param_grid.values())]
                total_combos = len(combos)

                all_results = []
                with ThreadPoolExecutor(max_workers=n_jobs if n_jobs > 0 else 1) as ex:
                    futures = {ex.submit(_single_pair_trial, c, idx): c for idx, c in enumerate(combos)}
                    for fut in as_completed(futures):
                        all_results.append(fut.result())

                all_results.sort(key=_weighted_score, reverse=True)
                best_metrics = all_results[0]
                best_params = best_metrics.get('params', {})
                best_ind = None
                if _has_next and ind_cls is not BtIndicator:
                    ind_cls.params = Addict({**(_orig_cls_params or {}), **best_params})
                    best_ind = ind_cls(_pair_data)
                else:
                    best_ind = self

                ls = getattr(best_ind, 'long_signal', None)
                ss = getattr(best_ind, 'short_signal', None)
                els = getattr(best_ind, 'exitlong_signal', None)
                exs = getattr(best_ind, 'exitshort_signal', None)
                post_e = np.zeros(len(close_arr))
                post_x = np.zeros(len(close_arr))
                if ls is not None:
                    post_e += ls.values if hasattr(ls, 'values') else ls
                if ss is not None:
                    post_e -= ss.values if hasattr(ss, 'values') else ss
                if els is not None:
                    post_x -= els.values if hasattr(els, 'values') else els
                if exs is not None:
                    post_x += exs.values if hasattr(exs, 'values') else exs
                if np.all(post_x == 0):
                    post_x = post_e

                if _has_valid_signals_idx(post_e):
                    t = _build_pair_strategy_result(
                        IndSeries(post_e, index=self.index) if isinstance(entries, IndSeries) else post_e,
                        IndSeries(post_x, index=self.index) if isinstance(entries, IndSeries) else post_x,
                        best_ind)
                else:
                    print(f"[警告] 最优参数未产生有效交易信号，跳过绘图。最优参数={best_params}")
                    t = None

                return OptimizationResult(best_params=best_params,
                                          best_score=_weighted_score(best_metrics),
                                          best_metrics=best_metrics,
                                          study={'results': all_results, 'total': total_combos},
                                          method='grid',
                                          target=targets, weights=_opt_weights, result=t)

            # ---- Optuna ----
            elif _opt_method == 'optuna':
                try:
                    import optuna
                except ImportError:
                    raise ImportError("请安装 optuna: pip install optuna")

                sampler_name = _cfg.get('sampler', 'TPESampler')
                pruner_name = _cfg.get('pruner', None)

                try:
                    sampler = getattr(optuna.samplers, sampler_name)()
                except AttributeError:
                    sampler = optuna.samplers.TPESampler()

                pruner = None
                if pruner_name:
                    try:
                        pruner = getattr(optuna.pruners, pruner_name)()
                    except AttributeError:
                        pass

                if pruner:
                    study = optuna.create_study(directions=directions, sampler=sampler, pruner=pruner)
                else:
                    study = optuna.create_study(directions=directions, sampler=sampler)

                def _optuna_objective(trial):
                    p = {}
                    for k, d in param_defs.items():
                        if d['type'] == 'cat':
                            p[k] = trial.suggest_categorical(k, d['choices'])
                        elif d['type'] == 'int':
                            p[k] = trial.suggest_int(k, d['low'], d['high'], step=d.get('step', 1))
                        else:
                            p[k] = trial.suggest_float(k, d['low'], d['high'], step=d.get('step'))
                    r = _single_pair_trial(p, trial.number)
                    if 'error' in r:
                        r.update({t: 0.0 for t in targets})
                    r['_trial_number'] = trial.number
                    trial.set_user_attr('_result', r)
                    return [r.get(t, 0.0) for t in targets]

                study.optimize(_optuna_objective, n_trials=n_trials, n_jobs=n_jobs if n_jobs > 0 else 1)

                # 多目标优化时 study.best_params / study.best_trial 不可用，需从 Pareto 前沿中选取
                if len(targets) == 1:
                    best_trial = study.best_trial
                else:
                    trials_sorted = sorted(study.best_trials, key=lambda t: t.values, reverse=True)
                    best_trial = trials_sorted[-1] if trials_sorted else None
                if best_trial is None:
                    raise RuntimeError("Optuna 未找到有效试验")
                best_params = best_trial.params
                best_trial_attrs = best_trial.user_attrs
                best_metrics = best_trial_attrs.get('_result', {}) if '_result' in best_trial_attrs else {}

                if _has_next and ind_cls is not BtIndicator:
                    ind_cls.params = Addict({**(_orig_cls_params or {}), **best_params})
                    best_ind = ind_cls(_pair_data)
                else:
                    best_ind = self

                ls = getattr(best_ind, 'long_signal', None)
                ss = getattr(best_ind, 'short_signal', None)
                els = getattr(best_ind, 'exitlong_signal', None)
                exs = getattr(best_ind, 'exitshort_signal', None)
                post_e = np.zeros(len(close_arr))
                post_x = np.zeros(len(close_arr))
                if ls is not None:
                    post_e += ls.values if hasattr(ls, 'values') else ls
                if ss is not None:
                    post_e -= ss.values if hasattr(ss, 'values') else ss
                if els is not None:
                    post_x -= els.values if hasattr(els, 'values') else els
                if exs is not None:
                    post_x += exs.values if hasattr(exs, 'values') else exs
                if np.all(post_x == 0):
                    post_x = post_e

                if _has_valid_signals_idx(post_e):
                    t = _build_pair_strategy_result(
                        IndSeries(post_e, index=self.index) if isinstance(entries, IndSeries) else post_e,
                        IndSeries(post_x, index=self.index) if isinstance(entries, IndSeries) else post_x,
                        best_ind)
                else:
                    print(f"[警告] 最优参数未产生有效交易信号，跳过绘图。最优参数={best_params}")
                    t = None

                return OptimizationResult(best_params=best_params,
                                          best_score=_weighted_score(best_metrics),
                                          best_metrics=best_metrics,
                                          study=study, method='optuna',
                                          target=targets, weights=_opt_weights, result=t)

            else:
                raise ValueError(f"不支持的优化方法: {_opt_method}，可选 'grid' 或 'optuna'")

        else:
            # 策略构建模式
            return _build_pair_strategy_result(entries, exits)


def _has_valid_signals_idx(arr):
    """检查信号数组是否有非零值"""
    if arr is None:
        return False
    vals = arr.values if hasattr(arr, 'values') else np.asarray(arr)
    return np.any(vals != 0)


@dataclass
class OptimizeConfig:
    """## 信号回测参数优化配置（DataClass）

    用于配置 :meth:`IndFrame.signal_backtest` 的参数优化模式。
    将实例传入 ``optimize`` 参数即可启用优化：``ind.signal_backtest(optimize=config)``。

    ### 参数说明：
    - ``params``：**必需**。待优化参数与取值范围，字典格式。
    - ``target``：优化目标指标名（默认 ``'sharpe'``）。
    - ``method``：优化方法 ``'grid'`` 或 ``'optuna'``（默认）。
    - ``weights``：优化目标权重（>0 最大化，<0 最小化）。不设置则最大化所有目标。
    - ``config``：优化器详细配置，见下表。
    - ``n_jobs``：并行线程数，-1 使用全部核心。
    - ``output_dir``：CSV 结果导出目录。

    ### ``params`` 格式说明：
    | 优化方法 | 格式 | 示例 | 说明 |
    |----------|------|------|------|
    | grid / optuna | ``(low, high)`` | ``(5, 30)`` | 仅两个整数 → 推断为 **int** 类型 |
    | grid / optuna | ``(low, high, step)`` | ``(5, 30, 2)`` | 三个数且均为整数 → 推断为 **int** 类型 |
    | grid / optuna | ``(low, high, step)`` | ``(1.5, 3.0, 0.5)`` | 含小数 → 推断为 **float** 类型 |
    | grid / optuna | ``[opt1, opt2, ...]`` | ``['a', 'b']`` | 列表（含字符串）→ **类别型** |

    > **类型推断规则**：当 ``low``、``high`` 及 ``step``（如有）全部为整数或等价于整数的浮点数（如 ``5.0``）时，参数类型自动推断为 ``int``；否则为 ``float``。因此 ``(5, 30)`` 会被正确识别为整数搜索区间，避免均线周期等参数出现小数。

    ### ``config`` 支持参数：
    | 参数 | 类型 | 默认值 | 适用方法 | 说明 |
    |------|------|--------|----------|------|
    | ``n_trials`` | ``int`` | ``100`` | optuna | 贝叶斯优化试验次数 |
    | ``sampler`` | ``str`` | ``'NSGAIISampler'`` | optuna | 采样器名称，可选 ``BaseSampler``、``GridSampler``、``RandomSampler``、``TPESampler``、``CmaEsSampler``、``PartialFixedSampler``、``NSGAIISampler``（多目标推荐，默认）、``NSGAIIISampler``、``MOTPESampler``、``QMCSampler``、``BruteForceSampler``、``IntersectionSearchSpace``、``intersection_search_space`` |
    | ``pruner`` | ``str`` / ``None`` | ``'HyperbandPruner'`` | optuna | 剪枝器名称，可选 ``BasePruner``、``MedianPruner``、``NopPruner``、``PatientPruner``、``PercentilePruner``、``SuccessiveHalvingPruner``、``HyperbandPruner``（默认）、``ThresholdPruner``；设为 ``None`` 关闭剪枝 |
    | ``verbose`` | ``bool`` | ``True`` | optuna | 是否输出 Optuna 详细日志 |
    | ``show_progress_bar`` | ``bool`` | ``True`` | optuna | 是否显示优化进度条 |
    | ``n_jobs`` | ``int`` | ``1`` | 通用 | 并行线程数；当 ``OptimizeConfig.n_jobs`` 为 ``1`` 时，优先取此值 |

    ### 使用示例：::

        # 网格搜索 + 单目标
        config = OptimizeConfig(
            params={'length1': (5, 30, 1), 'length2': (30, 60, 1)},
            target='sharpe', method='grid', n_jobs=4, output_dir='./results',
        )
        result = ind.signal_backtest(optimize=config)

        # Optuna 多目标 + 权重
        config = OptimizeConfig(
            params={'length1': (5, 30), 'length2': (30, 60)},
            target=['sharpe', 'max_drawdown'], weights=(1.0, -1.0),
            config={'n_trials': 100, 'sampler': 'TPESampler'},
        )
        result = ind.signal_backtest(optimize=config)
    """

    params: dict = field(default_factory=lambda:{})     # 待优化参数范围
    target: str | list[str] | OPTTargetType= 'sharpe'   # 优化目标指标
    method: Literal['grid', 'optuna']= 'optuna'         # 'grid' | 'optuna'
    weights: float | tuple[float] | None = None         # 优化权重
    config: dict[str,] | None = None                    # 优化器配置
    n_jobs: int = 1                                     # 并行线程数
    output_dir: str = ''                                # CSV 输出目录


class OptimizationResult:
    """## 信号回测优化结果容器
    
    用于存储 optimize=True 模式下的参数优化结果（网格搜索 / Optuna 贝叶斯优化）。
    通过 :meth:`IndFrame.signal_backtest` 方法在 optimize=True 时创建，无需手动实例化。

    ### 核心属性：
    - ``best_params``: 最优参数字典 (dict)
    - ``best_score``: 最优参数对应的主目标值 (float)
    - ``best_metrics``: 最优参数完整指标字典 (dict)
    - ``all_results``: 所有试验结果列表 (list[dict])，仅 grid 模式
    - ``study``: Optuna Study 对象，仅 optuna 模式
    - ``method``: 'grid' | 'optuna'
    - ``target``: 优化目标指标名（单目标时为 str，多目标时为 list[str]）
    - ``result``: 最优参数的 SignalBacktestResult 对象（可选）

    ### 分析接口：
    - ``.pprint()``: 格式化打印最优参数与指标
    - ``.to_dataframe()``: 返回试验结果 DataFrame（grid 模式）或 Optuna trials DataFrame
    - ``.plot_optuna()``: 绘制 Optuna 优化历史图表（仅 optuna 模式）
    """

    def __init__(self, best_params: dict, best_score, best_metrics: dict = None,
                 all_results: list = None, study=None, method: str = 'optuna',
                 target=None, weights=None, result=None):
        self.best_params = best_params
        self.best_score = best_score
        self.best_metrics = best_metrics or {}
        self.all_results = all_results or []
        self.study = study
        self.method = method
        self.target = target
        self.weights = weights
        self.result = result

    def pprint(self):
        """格式化打印最优参数与指标（类似 Strategy.pprint）"""
        from ..other import format_3col_report
        print(f"\n{'='*60}")
        target_str = self.target
        if self.weights is not None:
            if isinstance(self.target, (list, tuple)) and isinstance(self.weights, (list, tuple)):
                w_strs = [f"{t}({'max' if w>=0 else 'min'},|w|={abs(w):.1f})" 
                          for t, w in zip(self.target, self.weights)]
                target_str = f"[{', '.join(w_strs)}]"
            elif isinstance(self.weights, (float, int)):
                target_str = f"{self.target}({'max' if self.weights>=0 else 'min'},|w|={abs(self.weights):.1f})"
        print(f" 优化方法: {self.method.upper()}  |  目标: {target_str}")
        print(f"{'='*60}")
        print(" 最优参数:")
        for k, v in self.best_params.items():
            print(f"   {k}: {v}")
        print(f"{'='*60}")
        if self.best_metrics:
            display_metrics = {}
            for mk, mv in self.best_metrics.items():
                if mk in ('params', 'trial', 'equity', 'returns', 'error'):
                    continue
                if isinstance(mv, (int, float)):
                    display_metrics[mk] = mv
            if display_metrics:
                metrics_list = [(k, v, "{:.4f}") for k, v in display_metrics.items()]
                print(format_3col_report(metrics_list, "OptimizationResult"))
        elif self.best_score is not None:
            print(f" 加权最优值: {self.best_score:.4f}")
        print(f"{'='*60}")

    def to_dataframe(self) -> 'pd.DataFrame':
        """返回试验结果 DataFrame"""
        if self.study is not None:
            df = self.study.trials_dataframe(attrs=('number', 'value', 'params'))
            value_cols = [c for c in df.columns if 'value' in c]
            df.sort_values(by=value_cols, ascending=False, inplace=True, ignore_index=True)
            return df
        elif self.all_results:
            rows = []
            for r in self.all_results:
                row = {}
                if isinstance(self.target, (list, tuple)):
                    for t in self.target:
                        row[t] = r.get(t)
                else:
                    row[self.target] = r.get(self.target)
                row.update({k: v for k, v in r.get('params', {}).items()})
                rows.append(row)
            return pd.DataFrame(rows)
        return pd.DataFrame()

    def plot_optuna(self):
        """绘制 Optuna 优化历史图表（仅 optuna 模式有效）"""
        if self.method != 'optuna' or self.study is None:
            print("仅 Optuna 模式支持绘制优化图表")
            return
        try:
            import optuna.visualization
            if isinstance(self.target, (list, tuple)):
                optuna.visualization.plot_pareto_front(self.study).show()
            else:
                optuna.visualization.plot_optimization_history(self.study).show()
                optuna.visualization.plot_param_importances(self.study).show()
        except Exception as e:
            print(f"Optuna 图表绘制失败: {e}")

    def __repr__(self):
        params_str = ', '.join(f'{k}={v}' for k, v in list(self.best_params.items())[:5])
        if len(self.best_params) > 5:
            params_str += '...'
        return (f"OptimizationResult(method={self.method}, target={self.target}, "
                f"best_score={self.best_score}, params={{{params_str}}})")


class SignalBacktestResult:
    """## 信号回测结果容器

    提供统计分析、格式化打印、绘图报告等功能，完全独立于 Strategy/Bt 框架。
    通过 :meth:`IndFrame.signal_backtest` 方法创建，无需手动实例化。

    ### 核心属性：
    - ``profits``: 权益曲线（pd.Series，已对齐时间索引）
    - ``result_df``: 逐笔回测明细 DataFrame（含持仓、盈亏、手续费等）
    - ``init_cash`` / ``total_fee`` / ``total_profit``: 初始资金 / 总手续费 / 总收益
    - ``kline``: 关联的KLine对象
    - ``params``: 回测参数字典

    ### 分析接口：
    - ``.stats``: 返回 :class:`Stats` 统计分析对象
    - ``.qs_plots``: 返回 :class:`QSPlots` 绘图对象
    - ``.pprint()``: 格式化打印核心统计指标（类似 Strategy.pprint）
    - ``.plot()``: 返回 QSPlots 用于绘制收益曲线
    - ``.report(output, show)``: 生成 QuantStats HTML 分析报告
    """

    def __init__(self, 
            result_df: pd.DataFrame, 
            profits:pd.Series, 
            init_cash:float, 
            total_fee:float,
            total_profit:float, 
            kline:KLine, 
            params:dict):
        self.result_df = result_df
        self.profits = profits
        self.init_cash = init_cash
        self.total_fee = total_fee
        self.total_profit = total_profit
        self.kline = kline
        self.params = params
        self._stats_obj = None
        self._qs_plots_obj = None
        self._net_worth_cache = None
        self._net_worth()
        self.qs_plots()
        self.stats()

    def _net_worth(self):
        """日度收益率序列（QuantStats 标准输入格式）"""
        if self._net_worth_cache is None:
            nw = self.profits.pct_change()
            nw.iloc[0] = 0.
            index=pd.Index(self.kline.datetime.values)
            nw.index=index
            self._net_worth_cache = nw
        return self._net_worth_cache

    def stats(self):
        """获取 :class:`Stats` 统计分析对象，可调用多种指标方法"""
        if self._stats_obj is None:
            from ..strategy.stats import Stats
            self._stats_obj = Stats(
                self.profits,
                index=self.profits.index,
                name='profit',
                available=self.init_cash,
            )
        return self._stats_obj

    def qs_plots(self):
        """获取 :class:`QSPlots` 绘图对象"""
        if self._qs_plots_obj is None:
            from ..strategy.qs_plots import QSPlots
            self._qs_plots_obj = QSPlots(
                self.profits,
                index=self.profits.index,
                name='net_worth',
            )
        return self._qs_plots_obj

    def pprint(self):
        """## 格式化打印回测核心统计指标（类似 ``Strategy.pprint``）

        基于 quantstats 库计算15项核心指标并通过 :func:`format_3col_report` 格式化输出：
        - 收益相关：final return, commission, compounded
        - 风险相关：sharpe, risk, risk/return, max_drawdown
        - 质量相关：profit_factor, profit_ratio, win_rate
        - 频率/幅度：wins, losses, avg_return, avg_win, avg_loss

        若收益曲线无变化（可能无交易发生），输出提示信息。
        """
        # 兼容不同版本的 quantstats API
        try:
            from quantstats import stats as qs_stats  # quantstats >= 1.0
        except ImportError:
            import quantstats as qs_stats  # 旧版回退

        profits = pd.Series(self.profits).diff().fillna(0.)
        returns = self._net_worth()

        if len(profits.unique()) <= 1:
            print("无有效收益数据（收益曲线无变化），回测可能无交易发生")
            return

        final_return = profits.sum()
        comm = self.total_fee
        compounded = qs_stats.comp(returns)
        sharpe = qs_stats.sharpe(returns)
        max_dd = qs_stats.max_drawdown(returns)
        value_at_risk = qs_stats.value_at_risk(returns)
        risk_return_ratio = qs_stats.risk_return_ratio(returns)
        profit_factor = qs_stats.profit_factor(returns)
        profit_ratio = qs_stats.profit_ratio(returns)
        win_rate = qs_stats.win_rate(returns)
        wins = len(profits[profits > 0.])
        losses = len(profits[profits < 0.])
        avg_return = qs_stats.avg_return(profits)
        avg_win = qs_stats.avg_win(profits)
        avg_loss = qs_stats.avg_loss(profits)

        from ..other import format_3col_report

        metrics = [
            ("final return", final_return, "{:.2f}"),
            ("commission", comm, "{:.2f}"),
            ("compounded", compounded, "{:.2%}"),
            ("sharpe", sharpe, "{:.4f}"),
            ("risk", value_at_risk, "{:.4f}"),
            ("risk/return", risk_return_ratio, "{:.4f}"),
            ("max_drawdown", abs(max_dd), "{:.4%}"),
            ("profit_factor", profit_factor, "{:.4f}"),
            ("profit_ratio", profit_ratio, "{:.4f}"),
            ("win_rate", win_rate, "{:.4%}"),
            ("wins", wins, "{:d}"),
            ("losses", losses, "{:d}"),
            ("avg_return", avg_return, "{:.6f}"),
            ("avg_win", avg_win, "{:.6f}"),
            ("avg_loss", avg_loss, "{:.6f}"),
        ]
        print(format_3col_report(metrics, "SignalBacktest"))

    def plot(self):
        """## 返回QSPlots绘图对象

        可调用该对象上的 quantstats 绘图方法：
        - ``.returns()``: 收益率曲线
        - ``.drawdown()``: 回撤曲线
        - ``.histogram()``: 收益率分布直方图
        - ``.monthly_heatmap()``: 月度收益热力图
        - ``.rolling_sharpe()``: 滚动夏普比率
        - ``.yearly_returns()``: 年度收益率
        - ``.distribution()``: 收益分布图

        Returns:
            QSPlots: quantstats 绘图对象
        """
        return self.qs_plots

    def report(self, output=None, show=False):
        """## 生成 QuantStats HTML 分析报告

        基于 quantstats 库生成完整的量化分析报告（HTML格式），包含：
        - 收益表现分析
        - 风险指标分析
        - 交易行为分析
        - 可视化图表（收益曲线、回撤曲线等）

        Args:
            output (str, optional): 输出文件路径。默认保存至当前目录的 ``signal_backtest_report.html``
            show (bool): 生成后是否在浏览器中自动打开。默认 False
        """
        import os

        if output is None:
            output = os.path.join(os.getcwd(), 'signal_backtest_report.html')

        out_dir = os.path.dirname(output)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        import quantstats.reports as qs_reports
        qs_reports.html(self._net_worth, output=output)

        print(f"报告已保存至: {output}")

        if show:
            import webbrowser
            webbrowser.open(f"file://{output}")

    def __repr__(self):
        return (
            f"SignalBacktestResult(\n"
            f"  init_cash={self.init_cash:,.0f},\n"
            f"  final_equity={self.total_profit:,.2f},\n"
            f"  total_return={self.total_profit - self.init_cash:,.2f}, "
            f"({(self.total_profit / self.init_cash - 1) * 100:.2f}%),\n"
            f"  total_fee={self.total_fee:,.2f},\n"
            f"  n_bars={len(self.profits)}\n"
            f")"
        )


class KLine(IndFrame):
    """## 框架内置K线数据核心类（继承 IndFrame 类）
    - 核心定位：封装标准化K线数据（OHLCV等），整合合约信息、交易执行、风险控制、周期转换等量化交易全流程能力，是回测与实盘的核心数据载体

    ### 📘 **文档参考**:
    - 类简介：https://www.minibt.cn/minibt_basic/1.11minibt_internal_data_btdata_guide/

    ### 核心特性：
    1. K线数据标准化：
    - 强制要求输入数据包含 `FILED.ALL` 定义的所有核心字段（datetime/open/high/low/close/volume/symbol等）
    - 自动补充合约基础信息（最小变动单位 `price_tick`、合约乘数 `volume_multiple` 等），实盘模式自动从TqApi获取，回测模式用默认配置
    2. 多模式兼容：
    - 回测模式：关联 `Broker` 类管理虚拟账户、持仓、手续费、保证金等
    - 实盘模式：对接天勤TqApi，自动关联 `TqObjs`（合约报价、持仓、目标仓位任务）
    3. 交易能力原生集成：
    - 直接提供开仓（`buy`/`sell`）、目标仓位设置（`set_target_size`）等交易接口
    - 内置手续费（固定/百分比/按tick）、滑点（`slip_point`）、保证金率（`margin_rate`）配置
    4. 风险控制工具：
    - 支持止损止盈（`_set_stop` 绑定 `Stop` 停止器），自动生成止损止盈线（`stop_lines`）
    - 实时计算持仓浮动盈亏（`float_profit`）、持仓成本（`cost_price`）、保证金（`margin`）等风险指标
    5. 数据增强与转换：
    - 支持K线周期转换（`resample` 重采样、`replay` 回放），回测模式下可自定义周期规则
    - 内置特殊K线生成（Heikin-Ashi布林带K线、Linear Regression线性回归K线）
    6. 因子分析支持：
    - 提供 `factors_analyzer` 方法，支持多因子（如技术指标）的标准化、去极值处理，以及与收益率的关联分析
    7. 可视化配置：
    - 继承 `IndFrame` 的绘图配置能力，自动适配蜡烛图样式（涨跌颜色 `bull_color`/`bear_color`）
    - 实盘/回测模式下自动同步K线绘图数据，无需额外配置

    ### 初始化参数说明：
    Args:
        data (pd.DataFrame): 输入K线数据，必须满足：
                            - 类型为 pd.DataFrame
                            - 列名包含 `FILED.ALL` 定义的所有核心字段（如 datetime、open、high、low、close、volume、symbol、duration 等）
        **kwargs: 额外配置参数（核心参数如下）：
                - follow (bool): 是否跟随主图显示（默认 True，蜡烛图类指标生效）
                - plot_index (list[int]): 绘图时的索引范围（默认 None，显示全部数据）
                - kline_object/source_object/conversion_object: 数据副本分类（分别存储原始K线、源数据、转换后数据，默认自动生成）
                - tq_object: 实盘模式下的TqApi数据对象（默认 None，自动关联）
                - isindicator (bool): 是否标记为指标（固定为 False，因 KLine 是K线数据而非指标）

    ### 核心属性说明：
    1. 基础K线数据（自动从输入数据封装为 `IndSeries` 类型）：
    - datetime: 时间序列（格式统一为 "%Y-%m-%d %H:%M:%S"）
    - open/high/low/close: 开盘价/最高价/最低价/收盘价序列
    - volume: 成交量序列
    2. 合约信息（`symbol_info` 为 `SymbolInfo` 实例）：
    - symbol: 合约名称（如 "SHFE.rb2410"）
    - cycle: K线周期（单位：秒，如 60 表示1分钟线）
    - price_tick: 最小变动单位（如 0.01 元/吨）
    - volume_multiple: 合约乘数（每手对应标的物数量，如 10 吨/手）
    3. 交易与风险相关：
    - account: 关联的账户对象（回测为 `BtAccount`，实盘为 `TqAccount`）
    - broker: 回测模式下的交易代理（`Broker` 实例，管理订单、持仓、手续费）
    - position: 持仓对象（回测为 `BtPosition`，实盘为 `TqPosition`）
    - stop: 止损止盈停止器（`Stop` 实例，需通过 `_set_stop` 绑定）
    - stop_lines: 止损止盈线数据（`IndFrame` 类型，含 stop_price/target_price 列）
    4. 实盘专属属性：
    - quote: 实盘实时报价（`Quote` 实例，含最新价格、买一卖一等）
    - TargetPosTask: 实盘目标仓位任务（需传入仓位大小，返回 TqApi 的 TargetPosTask 实例）
    5. 状态与配置：
    - current_close/current_datetime: 当前周期的收盘价/时间（随回测/实盘进度动态更新）
    - Heikin_Ashi_Candles/Linear_Regression_Candles: 是否启用特殊K线（布尔值，赋值后自动生成对应K线）

    ### 核心方法说明：
    1. 交易执行：
    - buy(size=1, stop=None, **kwargs): 多头开仓（size为手数，stop为绑定的止损器）
    - sell(size=1, stop=None, **kwargs): 空头开仓/多头平仓（逻辑由策略实例统一管理）
    - set_target_size(size=0, stop=None): 设置目标仓位（0 表示平仓，正数为多仓，负数为空仓）
    2. 数据转换：
    - resample(cycle, rule=None, **kwargs): K线周期重采样（如1分钟线转5分钟线，回测模式生效）
    - replay(cycle, rule=None, **kwargs): K线周期回放（模拟实时数据推送，回测模式生效）
    3. 因子分析：
    - factors_analyzer(*factors, periods=[1,3,5,10], n_groups=5, **kwargs): 多因子分析（支持去极值、标准化，输出因子与收益率关联数据）
    4. 快速启动：
    - run(*args, **kwargs): 快速创建策略并启动回测（传入指标配置、交易逻辑 `next` 函数，返回 `Bt` 实例）

    ### 使用示例：
    >>> #1. 回测模式：从本地数据初始化K线
    >>> self.data = self.get_kline(LocalDatas.test)

    >>> #2. 配置手续费与保证金
    >>> kline.fixed_commission = 1.5  # 每手固定手续费1.5元
    >>> kline.margin_rate = 0.08      # 保证金率8%

    >>> #3. 绑定止损器（假设Stop为自定义停止器类）
    >>> self.data.buy(stop=BBtStop.TimeSegmentationTracking)

    >>> # 4. 实盘模式：通过TqApi关联合约（需先初始化TqApi）
    >>> self.data.set_target_size(2)  # 实盘设置目标多仓2手

    >>> # 5. 快速启动回测
    >>> def next(strategy):
    ...     # 简单均线交叉策略：5日线穿10日线开多
    ...     if strategy.ma5.new > strategy.ma10.new and strategy.ma5.prev < strategy.ma10.prev:
    ...         strategy.kline.buy(size=1)
    >>> # 初始化并启动
    >>> bt = kline.run(
    ...     ["ma5", Multiply(PandasTa.sma, dict(length=5))],
    ...     ["ma10", Multiply(PandasTa.sma, dict(length=10))],
    ...     next=next
    ... )

    """
    _kline_setting: KLineSetting
    # 交易代理（关联Broker，处理下单、手续费计算等）
    _broker: Broker | None
    _cooldown_entry_time: datetime  # 冷却时间结束时间
    _trading_time_filter: TradeTimeFilter | None  # 交易时间过滤器

    def __init__(self, data: pd.DataFrame | dict, **kwargs) -> None:
        if isinstance(data, dict):
            data = pd.DataFrame(data)
        assert isinstance(data, pd.DataFrame), "参数data数据格式只能为pd.DataFrame或dict"
        assert set(data.columns).issuperset(
            FILED.ALL), f"data数据列名至少包括{FILED.ALL}"
        if hasattr(data, "pandas_object"):
            data = data.pandas_object
        data.loc[:, FILED.OHLCV] = data.loc[:,
                                            FILED.OHLCV].astype(np.float64).values
        if self._is_live_trading:
            symbol, duration = data.symbol.iloc[0], data.duration.iloc[0]
            if symbol not in self._tqobjs:
                self._tqobjs.update({symbol: TqObjs(symbol)})
            tqobj = self._tqobjs[symbol]
            data.add_info(**SymbolInfo(
                symbol, int(duration), tqobj.Quote.price_tick, tqobj.Quote.volume_multiple).vars)
        elif not set(data.columns).issuperset(FILED.Quote):
            data.add_info(**default_symbol_info(data))
        data = data[FILED.Quote]
        data.datetime= pd.to_datetime(data.datetime)
        * _, symbol, cycle, price_tick, volume_multiple = data.loc[data.index[0]]
        # kwargs.update(dict(isindicator=False))
        super().__init__(data[FILED.ALL], **kwargs)
        symbol_info = SymbolInfo(
            symbol, cycle, price_tick, volume_multiple)
        isstop: bool = False
        stop: Stop = None
        stop_lines = None
        current_open = self.pandas_object.open.values
        current_high = self.pandas_object.high.values
        current_low = self.pandas_object.low.values
        current_close = self.pandas_object.close.values
        current_time = self.pandas_object.datetime
        current_datetime = current_time.dt.strftime(
            "%Y-%m-%d %H:%M:%S").values
        tradable: bool = True
        istrader: bool = False
        follow: bool = kwargs.pop("follow", True)
        plot_index: list[int] | None = kwargs.pop("plot_index", None)
        self._klinesetting = KLineSetting(
            symbol_info,
            current_open,
            current_high,
            current_low,
            current_close,
            current_datetime,
            current_time,
            isstop,
            stop,
            stop_lines,
            tradable,
            istrader,
            follow,
            plot_index,
        )
        self._indsetting.isindicator = False
        self._dataset = DataFrameSet(data[FILED.ALL], kline_object=kwargs.pop("kline_object", data), source_object=kwargs.pop(
            "source_object", self), conversion_object=kwargs.pop("conversion_object", None), custom_object=kwargs.pop("custom_object", None),
            tq_object=kwargs.pop("tq_object", None), tq_tick=kwargs.pop("tq_tick", None), copy_object=data.copy()[FILED.ALL])
        self._plotinfo.set_default_candles(current_close[-1], self.height)
        self._source_index = kwargs.get("source_index", None)
        self._broker = None
        self._new_datetime: datetime.datetime | None = None
        self._light_chart_candles_up_color = False
        self._light_chart_candles_down_color = False
        self._cooldown_entry_time = pd.NaT  # 冷却时间结束时间
        self._trading_time_filter = None # 交易时间过滤器



        if not self._strategy_instances:
            self._plotinfo.sname = "KLine"
            self._plotinfo.ind_name = "KLine"

    @property
    def broker(self) -> Broker | None:
        """### 代理"""
        return self._broker

    @property
    def orders(self) -> Orders | list[Order] | None:
        """### 订单"""
        if self._broker is None:
            return
        return self._broker._pending_orders
    
    @property
    def exectype(self) -> OrderType | None:
        """### 订单执行类型"""
        if self._broker is None:
            return
        return self._broker._default_exectype
    
    @exectype.setter
    def exectype(self, value: OrderType):
        if self._broker is None:
            return
        if value not in OrderType:
            return
        self._broker._default_exectype = value

    def _set_broker(self):
        if not self._is_live_trading and self._strategy_instances:
            if self._source_index is not None:
                self._broker = self.strategy_instance.account.brokers[self._source_index]
            else:
                self._broker = Broker(self)

    def factors_analyzer(self, *factors: BtIndType, periods=[1, 3, 5, 10], n_groups=5, winsorize=True, standardize=True, **kwargs):
        if not factors:
            return
        factors_df = pd.DataFrame()
        factors_df["datetime"] = pd.to_datetime(self.pandas_object.datetime)
        for factor in factors:
            if type(factor) in BtIndType:
                lines = factor.lines
                if len(lines) == 1:
                    factors_df[lines[0]] = factor.values
                else:
                    factors_df = pd.concat([factors_df, factor], axis=1)

        if len(factors_df.columns) <= 1:
            return
        single = False
        lines = list(factors_df.columns)
        lines.pop(0)
        analysis_type = 'multi'
        if len(lines) == 1:
            single = True
            lines = lines[0]
            analysis_type = 'single'

        prices = self.close.values
        returns = pd.Series(prices).pct_change()
        factors_df['return'] = returns.values
        factors_df.fillna(0., inplace=True)
        fitl_cols = ["datetime", "return"]
        if winsorize or standardize:
            factors_df = factors_df.copy()
            # 去极值（Winsorization）
            if winsorize:
                for col in factors_df.columns:
                    if col not in fitl_cols:
                        q_low = factors_df[col].quantile(0.05)
                        q_high = factors_df[col].quantile(0.95)
                        factors_df.loc[:, col] = factors_df[col].clip(
                            q_low, q_high)

            # 标准化
            if standardize:
                from sklearn.preprocessing import StandardScaler
                scaler = StandardScaler()
                factor_cols = [
                    col for col in factors_df.columns if col not in fitl_cols]
                factors_df.loc[:, factor_cols] = scaler.fit_transform(
                    factors_df[factor_cols])
        factors_df.lines = lines
        factors_df.analysis_type = analysis_type
        factors_df.periods = periods
        factors_df.n_groups = n_groups
        self.factors_df = factors_df

    def _inplace_values(self):
        """### 实时更新函数"""
        new_datas = self._dataset.tq_object.copy()[FILED.ALL]
        new_datas.datetime = new_datas.datetime.apply(time_to_datetime)
        # new_datas: pd.DataFrame = new_datas[new_datas["datetime"]
        #                                     >= self.pandas_object["datetime"].iloc[-1]]
        # new_datas.reset_index(drop=True, inplace=True)
        # pandas_object = pd.concat(
        #     [self._dataset.pandas_object.iloc[:-1], new_datas], axis=0, ignore_index=True)
        new_datas.reset_index(drop=True, inplace=True)
        self._update_replace(new_datas)

    def _reset_kline(self):
        self._update_replace(self._dataset.copy_object.copy()[FILED.ALL])

    @property
    def follow(self) -> bool:
        """## 如果是特殊蜡烛图指标，根据K线生成的指标是否跟随特殊蜡烛图指标
        - False :生成的所有指标用的是实际K线数据
        - True :生成的所有指标用的是特殊蜡烛图指标数据"""
        if not self.category in CandlesCategory:
            return True
        return self._klinesetting.follow

    @follow.setter
    def follow(self, value: bool):
        self._klinesetting.follow = bool(value)

    @property
    def current_open(self) -> float | None:
        """## 当前开盘价"""
        if self.btindex < self.length:
            return self._klinesetting.current_open[self.btindex]

    @property
    def current_high(self) -> float | None:
        """## 当前开盘价"""
        if self.btindex < self.length:
            return self._klinesetting.current_high[self.btindex]

    @property
    def current_low(self) -> float | None:
        """## 当前收盘价"""
        if self.btindex < self.length:
            return self._klinesetting.current_low[self.btindex]

    @property
    def current_close(self) -> float | None:
        """## 当前收盘价"""
        if self.btindex < self.length:
            return self._klinesetting.current_close[self.btindex]

    @property
    def current_datetime(self) -> str | None:
        """## 当前日期"""
        if self.btindex < self.length:
            return self._klinesetting.current_datetime[self.btindex]

    @property
    def current_time(self) -> datetime.datetime | None:
        """## 当前日期"""
        if self.btindex < self.length:
            return self._klinesetting.current_time[self.btindex]

    @property
    def symbol_info(self) -> SymbolInfo:
        """## 合约信息"""
        return self._klinesetting.symbol_info

    @property
    def symbol(self) -> str:
        """## 合约名称"""
        return self._klinesetting.symbol_info.symbol

    @property
    def cycle(self) -> int:
        """## 合约周期"""
        return self._klinesetting.symbol_info.cycle

    @property
    def price_tick(self) -> float:
        """## 合约最小变动单位"""
        if self._is_live_trading:
            return self._tqobjs[self.symbol].Quote.price_tick
        return self._klinesetting.symbol_info.price_tick

    @price_tick.setter
    def price_tick(self, value: float):
        """## 合约最小变动单位"""
        if self._is_live_trading:
            return
        if isinstance(value, (float, int)) and value > 0.:
            self._klinesetting.symbol_info.price_tick = float(value)
            if self._broker is None:
                return
            self._broker.price_tick = float(value)

    @property
    def volume_multiple(self) -> float:
        """## 合约剩数,即合约最小波动单位的价值"""
        if self._is_live_trading:
            return self._tqobjs[self.symbol].Quote.volume_multiple
        return self._klinesetting.symbol_info.volume_multiple

    @volume_multiple.setter
    def volume_multiple(self, value: float):
        """## 合约剩数,即合约最小波动单位的价值"""
        if self._is_live_trading:
            return
        if isinstance(value, (float, int)) and value > 0.:
            self._klinesetting.symbol_info.volume_multiple = float(value)
            if self._broker is None:
                return
            self._broker.volume_multiple = float(value)

    @property
    def quote(self) -> Quotes | Quote:
        if self.islivetrading:
            return self._tqobjs[self.symbol].Quote
        else:
            return Quotes(self.datetime.new, self.open.new, self.high.new, self.low.new, self.close.new, self.volume.new, self.price_tick, self.volume_multiple, self.volume_multiple)

    def TargetPosTask(self, size: int):
        """## 实盘时使用"""
        if self._is_live_trading:
            return self._tqobjs[self.symbol].TargetPosTask(size)

    @property
    def commission_dict(self) -> dict | None:
        """## 手续费"""
        if self._broker is None:
            return
        return self._broker.commission

    @property
    def commission(self) -> float | None:
        """## 手续费"""
        if self._broker is None:
            return
        return list(self._broker.commission.values())[0]

    @property
    def tick_commission(self) -> float | None:
        """## 每手手续费为波动一个点的价值的倍数"""
        if self._broker is None:
            return
        return self._broker.commission.get("tick_commission", None)

    @tick_commission.setter
    def tick_commission(self, value: float):
        if self._broker is None:
            return
        if isinstance(value, (float, int)) and value >= 0.:
            self._broker._setcommission(dict(tick_commission=float(value)))

    @property
    def percent_commission(self) -> float | None:
        """## 每手手续费为每手价值的百分比"""
        if self._broker is None:
            return
        return self._broker.commission.get("percent_commission", None)

    @percent_commission.setter
    def percent_commission(self, value: float):
        if self._broker is None:
            return
        if isinstance(value, (float, int)) and value >= 0.:
            self._broker._setcommission(dict(percent_commission=float(value)))

    @property
    def fixed_commission(self) -> float | None:
        """## 每手手续费为固定手续费"""
        if self._broker is None:
            return
        return self._broker.commission.get("fixed_commission", None)

    @fixed_commission.setter
    def fixed_commission(self, value: float):
        if self._broker is None:
            return
        if isinstance(value, (float, int)) and value >= 0.:
            self._broker._setcommission(dict(fixed_commission=float(value)))

    @property
    def commission_type(self) -> int | None:
        """## 手续费类型"""
        if self._broker is None:
            return
        if not self._broker.commission:
            return
        key=list(self._broker.commission.keys())[0]
        
        from ..other import CommissionType
        return getattr(CommissionType,key.capitalize(),None)
        

    @property
    def slip_point(self) -> float | None:
        """## 每手手续费为固定手续费"""
        if self._broker is None:
            return
        return self._broker.slip_point

    @slip_point.setter
    def slip_point(self, value: float):
        if self._broker is None:
            return
        if isinstance(value, (float, int)) and value >= 0.:
            self._broker.slip_point = value

    def _set_stop(self, stop: Stop, **kwargs) -> KLine:
        """## 设置停止器

        Args:
            stop (Stop, optional): 停止器. Defaults to None.

        Kwargs:
            stop_plot (bool, optional): 停止线是否画图. Defaults to True.
            data_length (int):引用数据的长度. Defaults to 300.

        Returns:
            KLine
        """
        if isinstance(stop, ProcessedAttribute):
            stop = stop()
        if stop and (issubclass(stop.func, Stop) if isinstance(stop, partial) else issubclass(stop, Stop)):
            self._klinesetting.isstop = True
            self._klinesetting.set_default_stop_lines(self.id)
            
            # 确保 KLine 对象正确关联到策略实例
            kline_for_stop = self
            if self.strategy_instance is not None and hasattr(self.strategy_instance, '_btklinedataset'):
                # 获取策略实例中正确关联的 KLine 对象
                _kline = self.strategy_instance._btklinedataset.get(self.id.data_id)
                if _kline is not None:
                    kline_for_stop = _kline
            
            self._klinesetting.stop = stop(kline_for_stop, **kwargs)
            if self.strategy_instance is not None:
                self.strategy_instance._isstop = True
        return self

    @property
    def stop(self) -> Stop | None:
        """## 停止器"""
        return self._klinesetting.stop

    @property
    def stop_lines(self) -> IndFrame | None:
        """## 停止线"""
        return self._klinesetting.stop_lines

    @property
    def stop_price(self) -> IndSeries | None:
        """## 停止价"""
        if self._klinesetting.stop_lines is None:
            return
        return self._klinesetting.stop_lines.stop_price

    @property
    def target_price(self) -> IndSeries | None:
        """## 目标价"""
        if self._klinesetting.stop_lines is None:
            return
        return self._klinesetting.stop_lines.target_price

    @property
    def account(self) -> BtAccount | TqAccount:
        """## 策略账户"""
        return self.strategy_instance._account

    def buy(self,
            size: int = 1,
            exectype: OrderType = OrderType.Close,
            price: float = None,
            valid: datetime | timedelta | int | None = None,
            stop=None) -> Order | float:
        """
        ## 通过KLine对象执行买入操作

        - 便捷方法，通过KLine对象直接调用关联策略的买入接口

        Args:
            data (KLine, optional): 目标合约数据
            size: (int) 买入手数
            exectype (OrderType): 订单类型（Market, Limit, Stop等）
            price (float): 委托价格（限价单/止损单需要）
            valid (datetime.datetime | datetime.timedelta | float): 订单有效期
            stop (BtStop): 停止器设置
            **kwargs: 其他参数
                - bar (int): 1

        Returns:
        >>> float | None: 交易盈亏（回测模式）或浮动盈亏（实盘模式）

        ### 示例:
        ```python
        # Example: 在当前K线买入1手
        kline.buy(size=1)

        # Example: 买入2手并设置止损
        kline.buy(size=2, stop=BtStop.SegmentationTracking)
        ```
        ### Note:
        - 实际执行逻辑由关联的strategy_instance.buy方法处理
        - 回测模式返回交易盈亏，实盘模式返回浮动盈亏
        - 需确保KLine已正确关联到策略实例
        """
        if not self._strategy_instances:
            return
        data: KLine = self.source_object if self.isresample or self.isreplay else self
        if data.isresample or data.isreplay:
            return data.buy(size, exectype, price, valid, stop)
        return self.strategy_instance.buy(data, size, exectype, price, valid, stop)

    def sell(self,
             size: int = 1,
             exectype: OrderType = OrderType.Close,
             price: float = None,
             valid: datetime | timedelta | int | None = None,
             stop=None) -> Order | float:
        """
        ## 通过KLine对象执行卖出操作

        - 便捷方法，通过KLine对象直接调用关联策略的卖出接口

        Args:
            data (KLine, optional): 目标合约数据
            size: (int) 买入手数
            exectype (OrderType): 订单类型（Market, Limit, Stop等）
            price (float): 委托价格（限价单/止损单需要）
            valid (datetime.datetime | datetime.timedelta | float): 订单有效期
            stop (BtStop): 停止器设置
            **kwargs: 其他参数
                - bar (int): 1

        Returns:
        >>> float | None: 交易盈亏（回测模式）或浮动盈亏（实盘模式）

        ### 示例:
        ```python
        # Example: 在当前K线卖出1手
        kline.sell(size=1)

        # Example: 卖出2手并设置止损
        kline.sell(size=2, stop=BtStop.SegmentationTracking)
        ```
        ### Note:
        - 实际执行逻辑由关联的strategy_instance.sell方法处理
        - 回测模式返回交易盈亏，实盘模式返回浮动盈亏
        - 需确保KLine已正确关联到策略实例
        """
        if not self._strategy_instances:
            return
        data = self.source_object if self.isresample or self.isreplay else self
        if data.isresample or data.isreplay:
            return data.buy(size, exectype, price, valid, stop)
        return self.strategy_instance.sell(data, size, exectype, price, valid, stop)

    def set_target_size(self, size: int = 0) -> None:
        """
        ## 通过KLine对象设置目标仓位

        - 便捷方法，通过KLine对象直接调用关联策略的目标仓位设置接口

        Args:
            size (int): 目标仓位手数
                - 正数: 多头仓位
                - 负数: 空头仓位
                - 0: 平仓

        Returns:
            None
        ### 示例:
        ```python
        # Example: 设置目标仓位为5手多头
        kline.set_target_size(size=5)

        # Example: 设置目标仓位为-3手（空头）
        kline.set_target_size(size=-3)

        # Example: 平仓
        kline.set_target_size(size=0)
        ```
        ### Note:
        - 实际执行逻辑由关联的strategy_instance.set_target_size方法处理
        - 系统会自动计算当前仓位与目标仓位的差值，执行相应交易
        - 需确保KLine已正确关联到策略实例
        """
        if not self._strategy_instances:
            return
        data = self.source_object if self.isresample or self.isreplay else self
        return self.strategy_instance.set_target_size(data=data, size=size)

    def insert_order(self, direction: OrderSide | int=OrderSide.Buy, offset: str = "", volume: int = 0,
                     limit_price: Union[str, float, None] = None, advanced: Optional[str] = None,
                     order_id: Optional[str] = None, account: Optional[UnionTradeable] = None) -> TqOrder | None:
        """
        ## 通过KLine对象发送下单指令，只在实盘模式下生效
        发送下单指令。调用后会立即返回，委托请求会在下一次 :py:meth:`~tqsdk.api.TqApi.wait_update` 时发出。

        Args:
            kline (KLine, optional): 目标合约数据. Defaults to None.
            direction (OrderSide | int, optional): 下单方向. Defaults to OrderSide.Buy ("BUY").

            offset (str, optional): "OPEN", "CLOSE" 或 "CLOSETODAY"  \
                (上期所和上期能源分平今/平昨, 平今用"CLOSETODAY", 平昨用"CLOSE"; 其他交易所直接用"CLOSE" 按照交易所的规则平仓), \
                股票交易中该参数无需填写

            volume (int): 下单交易数量, 期货为下单手数, A 股股票为股数

            limit_price (float | str): [可选] 下单价格, 默认为 None, 股票交易目前仅支持限价单, 该字段必须指定。
                * 数字类型: 限价单，按照限定价格或者更优价格成交
                * None: 市价单，默认值就是市价单 (郑商所期货/期权、大商所期货支持)
                * "BEST": 最优一档，以对手方实时最优一档价格为成交价格成交（仅中金所支持）
                * "FIVELEVEL": 最优五档，在对手方实时最优五个价位内以对手方价格为成交价格依次成交（仅中金所支持）

            advanced (str): [可选] "FAK", "FOK"。默认为 None, 股票交易中该参数不支持。
                * None: 对于限价单，任意手数成交，委托单当日有效；对于市价单、最优一档、最优五档(与 FAK 指令一致)，任意手数成交，剩余撤单。
                * "FAK": 剩余即撤销，指在指定价位成交，剩余委托自动被系统撤销。(限价单、市价单、最优一档、最优五档有效)
                * "FOK": 全成或全撤，指在指定价位要么全部成交，否则全部自动被系统撤销。(限价单、市价单有效，郑商所期货品种不支持 FOK)

            order_id (str): [可选]指定下单单号, 默认由 api 自动生成

            account (TqAccount/TqKq/TqKqStock/TqSim): [可选]指定发送下单指令的账户实例, 多账户模式下，该参数必须指定

        Returns:
            :py:class:`~tqsdk.objs.Order`: 返回一个委托单对象引用. 其内容将在 :py:meth:`~tqsdk.api.TqApi.wait_update` 时更新.

        Example1::

            # 市价开3手 DCE.m1809 多仓
            order = self.insert_order(kline=kline, direction=direction, offset="OPEN", volume=3)
            while True:
                self.api.wait_update()
                print("单状态: %s, 已成交: %d 手" % (order.status, order.volume_orign - order.volume_left))

            # 预计的输出是这样的:
            单状态: ALIVE, 已成交: 0 手
            单状态: ALIVE, 已成交: 0 手
            单状态: FINISHED, 已成交: 3 手
            ...

        Example2::

            # 限价开多3手 DCE.m1901
            order = self.insert_order(kline=kline, direction=direction, offset="OPEN", volume=3, limit_price=3000)
            while True:
                self.api.wait_update()
                print("单状态: %s, 已成交: %d 手" % (order.status, order.volume_orign - order.volume_left))

            # 预计的输出是这样的:
            单状态: ALIVE, 已成交: 0 手
            单状态: ALIVE, 已成交: 0 手
            单状态: FINISHED, 已成交: 3 手
            ...

        Example3::

            # 市价开多3手 DCE.m1901 FAK
            order = self.insert_order(kline=kline, direction=direction, offset="OPEN", volume=3, advanced="FAK")
            while True:
                self.api.wait_update()
                print("单状态: %s, 已成交: %d 手" % (order.status, order.volume_orign - order.volume_left))

            # 预计的输出是这样的:
            单状态: ALIVE, 已成交: 0 手
            单状态: ALIVE, 已成交: 0 手
            单状态: FINISHED, 已成交: 3 手
            ...
            
        Example4::

            order = self.insert_order(symbol="SHFE.rb2610", direction="BUY", offset="OPEN", limit_price=4310,volume=2)
            while order.status != "FINISHED":
                self.api.wait_update()
                print("委托单状态: %s, 未成交手数: %d 手" % (order.status, order.volume_left))
            
            # 要撤销一个委托单, 使用 cancel_order() 函数:
            self.api.cancel_order(order)
        
        ### 委托单状态信息
        >>> {
            "order_id": "",  # "123" (委托单ID, 对于一个用户的所有委托单，这个ID都是不重复的)
            "exchange_order_id": "",  # "1928341" (交易所单号)
            "exchange_id": "",  # "SHFE" (交易所)
            "instrument_id": "",  # "rb2610" (交易所内的合约代码)
            "direction": "",  # "BUY" (下单方向, BUY=买, SELL=卖)
            "offset": "",  # "OPEN" (开平标志, OPEN=开仓, CLOSE=平仓, CLOSETODAY=平今)
            "volume_orign": 0,  # 10 (总报单手数)
            "volume_left": 0,  # 5 (未成交手数)
            "limit_price": float("nan"),  # 4500.0 (委托价格, 仅当 price_type = LIMIT 时有效)
            "price_type": "",  # "LIMIT" (价格类型, ANY=市价, LIMIT=限价)
            "volume_condition": "",  # "ANY" (手数条件, ANY=任何数量, MIN=最小数量, ALL=全部数量)
            "time_condition": "",  # "GFD" (时间条件, IOC=立即完成，否则撤销, GFS=本节有效, GFD=当日有效, GTC=撤销前有效, GFA=集合竞价有效)
            "insert_date_time": 0,  # 1501074872000000000 (下单时间(按北京时间)，自unix epoch(1970-01-01 00:00:00 GMT)以来的纳秒数)
            "status": "",  # "ALIVE" (委托单状态, ALIVE=有效, FINISHED=已完)
            "last_msg": "",  # "报单成功" (委托单状态信息)
        }
        """
        if not self._strategy_instances:
            return
        return self.strategy_instance.insert_order(self, direction, offset, volume, limit_price, advanced, order_id, account)

    def in_cooldown(self, cooldown_seconds: float | int = 0.0,cooldown_bars: int | None = None) -> bool | None:
        """
        ## 检查是否在冷却中（开仓后 N 秒内禁止任何交易操作（包括平仓））
        Args:
            cooldown_seconds: 冷却时间（秒）
            cooldown_bars: 冷却K线数
        Returns:
            bool: 是否在冷却中
            
        Note:
            如果 cooldown_seconds 和 cooldown_bars 都为 None，则默认 cooldown_bars 优先级高。
        """
        if not self._strategy_instances:
            return
        return self.strategy_instance.in_cooldown(self,cooldown_seconds,cooldown_bars)
    
    def exclude_time(self, start: str, end: str) -> TradeTimeFilter:
        """
        ## 排除指定时间范围内的交易时间
        Args:
            start: 开始时间（格式：'HH:MM:SS'）,
            end: 结束时间（格式：'HH:MM:SS'）
        Returns:
            TradeTimeFilter: 交易时间过滤器对象
        """
        if self._trading_time_filter is None:
            self._trading_time_filter = TradeTimeFilter(self._tqobjs[self.symbol])
        self._trading_time_filter.exclude_time(start, end)
        return self._trading_time_filter
    
    def exclude_open_minutes(self, minutes: int) -> TradeTimeFilter:
        """
        ## 排除开仓后 N 分钟内的交易时间
        Args:
            minutes: 开仓后 N 分钟
        Returns:
            TradeTimeFilter: 交易时间过滤器对象
        """
        if self._trading_time_filter is None:
            self._trading_time_filter = TradeTimeFilter(self._tqobjs[self.symbol])
        self._trading_time_filter.exclude_open_minutes(minutes)
        return self._trading_time_filter
    
    def exclude_close_minutes(self, minutes: int) -> TradeTimeFilter:
        """
        ## 排除平仓后 N 分钟内的交易时间
        Args:
            minutes: 平仓后 N 分钟
        Returns:
            TradeTimeFilter: 交易时间过滤器对象
        """
        if self._trading_time_filter is None:
            self._trading_time_filter = TradeTimeFilter(self._tqobjs[self.symbol])
        self._trading_time_filter.exclude_close_minutes(minutes)
        return self._trading_time_filter
    
    def get_tradable_segments(self) -> list[tuple[int, int]]:
        """
        ## 获取可交易时间段
        Returns:
            list[tuple[int, int]]: 可交易时间段列表，每个元素为 (开始时间索引, 结束时间索引)
        """
        if self._trading_time_filter is None:
            self._trading_time_filter = TradeTimeFilter(self._tqobjs[self.symbol])
        return self._trading_time_filter.get_tradable_segments()
    
    def get_sessions_info(self) -> list[dict]:
        """
        ## 获取所有会话信息
        Returns:
            list[dict]: 所有会话信息列表，每个元素为一个字典，包含会话信息
        """
        if self._trading_time_filter is None:
            self._trading_time_filter = TradeTimeFilter(self._tqobjs[self.symbol])
        return self._trading_time_filter.get_sessions_info()
    
    def can_trade(self, current_time=None) -> bool:
        """
        ## 检查当前时间是否可交易
        Args:
            current_time: 当前时间（格式：'HH:MM:SS'）
        Returns:
            bool: 是否可交易
        """
        if self._trading_time_filter is None:
            self._trading_time_filter = TradeTimeFilter(self._tqobjs[self.symbol])
        return self._trading_time_filter.can_trade(current_time)
    
    @property
    def margin(self) -> float | None:
        """## 保证金"""
        if self._is_live_trading:
            return self.position.margin
        if self._broker is None:
            return
        return self._broker._getmargin(self.current_close)

    @property
    def step_margin(self) -> list[float] | float | None:
        """## 逐笔保证金"""
        if self._is_live_trading:
            return self.position.margin
        if self._broker is None:
            return
        return self._broker._step_margin

    @property
    def position_margin(self) -> float | None:
        """## 保证金"""
        if self._is_live_trading:
            return self.position.margin
        if self._broker is None:
            return
        return self._broker._margin

    @property
    def margin_rate(self) -> float | None:
        """## 保证金"""
        if not self._is_live_trading:
            if self._broker is None:
                return
            return self._broker.margin_rate

    @margin_rate.setter
    def margin_rate(self, value):
        if self._broker is None:
            return
        if not self._is_live_trading and isinstance(value, float) and 0. < value < 1.:
            self._broker.margin_rate = value

    @property
    def position(self) -> Position | BtPosition | None:
        """## 持仓对象"""
        if self._is_live_trading:
            return self._tqobjs[self.symbol].Position
        if self._broker is None:
            return
        return self._broker.position

    @property
    def float_profit(self) -> float | None:
        """## 持仓浮动盈亏"""
        if self._is_live_trading:
            return self.position.float_profit
        if self._broker is None:
            return
        return self._broker._float_profit

    @property
    def float_tick(self) -> float | None:
        """## 持仓浮动点数"""
        if self._is_live_trading:
            return self.float_profit/self.volume_multiple
        if self._broker is None:
            return
        return self.float_profit/self.volume_multiple

    @property
    def open_price(self) -> float | None:
        """## 合约开仓价"""
        if self._is_live_trading:
            pos = self.position.pos
            if pos > 0:
                return self.position.open_price_long
            elif pos < 0:
                return self.position.open_price_short
            else:
                return 0.
        if self._broker is None:
            return
        return self._broker._open_price

    @property
    def cost_price(self) -> float | None:
        """## 合约持仓成本价"""
        if self._is_live_trading:
            pos = self.position.pos
            if pos > 0:
                return self.position.position_price_long
            elif pos < 0:
                return self.position.position_price_short
            else:
                return 0.
        if self._broker is None:
            return
        return self._broker._cost_price

    @property
    def current_commission(self) -> float | None:
        """## 当前手续费"""
        if self._is_live_trading:
            return
        if self._broker is None:
            return
        return self._broker._comm

    def resample(self, cycle: int, data_length: int = None, rule: str = None, **kwargs) -> KLine:
        """## 周期转换

        Args:
            cycle (int): 转换周期,不能低于主周期.
            data_length (int): 数据长度.
            rule (str, optional): 日周期以上需要自行设置,D,W,M. Defaults to None.

        Returns:
        >>> KLine
        """
        if not self._strategy_instances:
            from ..strategy.strategy import Strategy
            # 选择原始数据（跟随主数据或使用K线原始数据）
            df = self.pandas_object if self.follow else self.kline_object
            main_cycle = self.cycle  # 原始主周期（秒）

            # 参数校验：目标周期必须大于原始周期且为倍数
            assert cycle > main_cycle and cycle % main_cycle == 0, '周期不能低于主周期并且为主周期的倍数'
            # 生成时间规则字符串（如300秒→"300S"，900秒→"15T"）
            cycle_string = rule if (isinstance(rule, str) and rule in ['D', 'W', 'M']) else \
                f"{cycle}S" if cycle < 60 else (
                    f"{int(cycle/60)}T" if cycle < 3600 else f"{int(cycle/3600)}H"
            )
            # 调用核心重采样逻辑
            plot_index, rdata = Strategy._resample(
                main_cycle, cycle, df[FILED.ALL], cycle_string)

            # 生成新的指标ID（关联主数据ID，标记为高周期数据）
            _id = 0
            id = self.id.copy(plot_id=_id, data_id=_id,
                              resample_id=self.data_id)

            # 补充合约信息（目标周期的合约参数）
            symbolinfo_dict = self.symbol_info.filt_values(duration=cycle)
            rdata.add_info(**symbolinfo_dict)

            # 配置参数：传递转换数据、绘图索引等
            kwargs.update(
                dict(
                    conversion_object=self.pandas_object if self.follow else self.kline_object,
                    plot_index=plot_index,
                    source_object=self,
                    source_index=self.data_id
                )
            )

            # 创建并返回高周期KLine实例（标记为isresample=True）
            return KLine(rdata, id=id, isresample=True, name=f"datas{_id}", **kwargs)
        if self._is_live_trading:
            data_length = data_length if data_length and data_length > 0 else 200
            return self.strategy_instance.get_kline(self.symbol, int(cycle), data_length, **kwargs)
        else:
            return self.strategy_instance.resample(int(cycle), self, rule, **kwargs)

    def replay(self, cycle: int, data_length: int = None, rule: str = None, **kwargs) -> KLine:
        """## 周期转换

        Args:
            cycle (int): 转换周期,不能低于主周期.
            data_length (int): 数据长度.
            rule (str, optional): 日周期以上需要自行设置,D,W,M. Defaults to None.

        Returns:
        >>> KLine
        """
        if not self._strategy_instances:
            from ..strategy.strategy import Strategy
            # 参数校验：周期必须为整数
            assert isinstance(cycle, int), "周期必须为整数"

            # 确定原始数据：默认使用主数据
            data = self
            main_cycle = data.cycle  # 原始高周期（秒）

            # 参数校验：目标周期必须大于原始周期且为倍数
            assert cycle > main_cycle and cycle % main_cycle == 0, '周期不能低于主周期并且为主周期的倍数'

            # 选择原始数据（跟随主数据或使用K线原始数据）
            df = data.pandas_object if data.follow else data.kline_object
            # 生成时间规则字符串
            cycle_string = rule if (isinstance(rule, str) and rule in ['D', 'W', 'M']) else \
                f"{cycle}S" if cycle < 60 else (
                    f"{int(cycle/60)}T" if cycle < 3600 else f"{int(cycle/3600)}H"
            )

            # 调用核心回放逻辑，生成低周期回放数据
            rdata = Strategy._replay(
                main_cycle, cycle, df[FILED.ALL], cycle_string)

            # 生成新的指标ID（关联主数据ID，标记为回放数据）
            _id = 0
            id = data.id.copy(plot_id=_id, data_id=_id, replay_id=data.data_id)

            # 补充合约信息（目标周期的合约参数）
            symbolinfo_dict = data.symbol_info.filt_values(duration=cycle)
            rdata.add_info(**symbolinfo_dict)

            # 生成重采样数据（用于回放时的时间对齐）
            plot_index, resample_data = Strategy._resample(
                main_cycle, cycle, df[FILED.ALL], cycle_string)
            resample_data.add_info(**symbolinfo_dict)
            resample_data = resample_data[FILED.Quote]

            # 配置参数：传递转换数据、绘图索引、源数据等
            kwargs.update(
                dict(
                    conversion_object=resample_data,
                    plot_index=plot_index,
                    source_object=data.pandas_object if data.follow else data.kline_object
                )
            )

            # 创建并返回回放后的KLine实例（标记为isreplay=True）
            return KLine(rdata, id=id, isreplay=True, name=f"datas{_id}", **kwargs)
        if self._is_live_trading:
            data_length = data_length if data_length and data_length > 0 else 200
            return self.strategy_instance.get_kline(self.symbol, int(cycle), data_length, **kwargs)
        else:
            return self.strategy_instance.replay(int(cycle), self, rule, **kwargs)

    def _replay_datas(self, keys: list[str] = "all") -> list[pd.DataFrame, pd.Series]:
        isany = keys != "all"
        one = False
        if isany:
            if isinstance(keys, str):
                one = True
            if len(keys) == 1:
                keys = keys[0]
                one = True
        datas = []
        for d in self.pandas_object.values:
            dt = d[0]
            data = self.conversion_object[self.conversion_object.datetime <= dt]
            data.loc[len(data)-1] = d
            if isany:
                data = data[keys]
            datas.append(data)
        return datas

    @property
    def Heikin_Ashi_Candles(self) -> bool:
        """## 黑金K线图"""
        return self.category == CandlesCategory.Heikin_Ashi_Candles

    @Heikin_Ashi_Candles.setter
    def Heikin_Ashi_Candles(self, value: bool) -> None:
        if value:
            _id = self.id.data_id
            source: corefunc = self.pandas_object.copy()
            kline_object = self.pandas_object.copy()
            if isinstance(value, int) and value > 0:
                df = source.ta.Heikin_Ashi_Candles(value)
            else:
                df = source.ta.ha()
            if not self._strategy_instances:
                self.loc[:, FILED.OHLC] = df.loc[:, FILED.OHLC].values
                self.category = Category.Linear_Regression_Candles
                return
            source.loc[:, FILED.OHLC] = df.loc[:, FILED.OHLC].values
            setting = self.get_indicator_kwargs()
            setting.update(
                dict(category=Category.Heikin_Ashi_Candles, kline_object=kline_object))
            source["symbol"] = self.symbol
            source["duration"] = self.cycle
            data = KLine(source, **setting)
            data._dataset = self._dataset
            setattr(self.strategy_instance, self.sname, data)

    @property
    def Linear_Regression_Candles(self) -> bool:
        """## 线性回归K线图"""
        return self.category == CandlesCategory.Linear_Regression_Candles

    @Linear_Regression_Candles.setter
    def Linear_Regression_Candles(self, value: int) -> None:
        value = value if isinstance(value, int) and value > 1 else 11
        if value:
            _id = self.id.data_id
            kline_object = self.pandas_object.copy()
            source: corefunc = self.pandas_object.copy()
            df = self.pandas_object.ta.Linear_Regression_Candles(length=value)
            if not self._strategy_instances:
                self.loc[:, FILED.OHLC] = df.loc[:, FILED.OHLC].values
                self.category = Category.Linear_Regression_Candles
                return
            source.loc[:, FILED.OHLC] = df.loc[:, FILED.OHLC].values
            setting = self.get_indicator_kwargs()
            setting.update(
                dict(category=Category.Linear_Regression_Candles, kline_object=kline_object))
            source["symbol"] = self.symbol
            source["duration"] = self.cycle
            data = KLine(source, **setting)
            data._dataset = self._dataset
            setattr(self.strategy_instance, self.sname, data)

    def _open_trader(self):
        self._istrader = any(
            [getattr(self, string) is not None for string in SIGNAL_Str])

    @property
    def watermark(self) -> WaterMark | None:
        """## 获取指标水印（属性接口）
        - 只适配lightweight_charts图表

        ### 设置水印:
        - bool ：True:默认水印,False:None
        - str : WaterMark(text)
        - WaterMark : WaterMark
        Returns:
            WaterMark | None
        """
        return self._plotinfo.watermark

    @watermark.setter
    def watermark(self, value: bool | str | WaterMark):
        if value is None:
            self._plotinfo.watermark = None
            return
        if isinstance(value, bool):
            if value:
                text = f"{self.symbol}_{self.cycle}"
                self._plotinfo.watermark = WaterMark(text)
            else:
                self._plotinfo.watermark = None
        elif isinstance(value, str):
            if value:
                self._plotinfo.watermark = WaterMark(value)
            else:
                self._plotinfo.watermark = None
        elif isinstance(value, WaterMark):
            self._plotinfo.watermark = value

    @property
    def iswatermark(self) -> bool:
        """## 是否设置了指标水印（属性接口）
        - 只适配lightweight_charts图表

        ### 可设置默认水印
        Returns:
            bool
        """
        return self._plotinfo.watermark is not None

    @iswatermark.setter
    def iswatermark(self, value):
        value = bool(value)
        if value:
            text = f"{self.symbol}_{self.cycle}"
            self._plotinfo.watermark = WaterMark(text)
        else:
            self._plotinfo.watermark = None

    @property
    def datetime(self) -> Line:
        """## 时间序列"""
        return getattr(self, '_datetime')

    @property
    def open(self) -> Line:
        """## 开盘价序列"""
        return getattr(self, '_open')

    @property
    def high(self) -> Line:
        """## 最高价序列"""
        return getattr(self, '_high')

    @property
    def low(self) -> Line:
        """## 最低价序列"""
        return getattr(self, '_low')

    @property
    def close(self) -> Line:
        """## 收盘价序列"""
        return getattr(self, '_close')

    @property
    def volume(self) -> Line:
        """## 成交量序列"""
        return getattr(self, '_volume')

    @property
    def bear_color(self) -> str:
        """
        ## 获取或设置下跌蜡烛图（阴线）颜色

        - 获取当前K线图中下跌蜡烛图（收盘价低于开盘价）的颜色设置
        - 设置K线图中下跌蜡烛图（收盘价低于开盘价）的显示颜色

        Args:
            value (str): 颜色值，支持以下格式：
                - Colors枚举值（如Colors.green）
                - 十六进制颜色字符串（如"#00FF00"）

        Returns:
            str: 下跌蜡烛图的颜色值，格式为十六进制颜色字符串或颜色名称

        Raises:
            ValueError: 当传入无效颜色值时不会生效但不会报错（静默忽略）
            AttributeError: 当蜡烛图样式未启用时访问会引发异常

        ### 示例
        ```python
        # 获取当前下跌蜡烛颜色
        color = kline.bear_color
        print(f"下跌蜡烛颜色: {color}")
        # 设置下跌蜡烛为绿色
        kline.bear_color = Colors.green
        # 使用十六进制颜色
        kline.bear_color = "#008800"
        ```
        ### Note:
        - 仅当蜡烛图样式启用时（_plotinfo.candlestyle存在）才可访问
        - 默认通常为绿色或红色，取决于图表主题配置
        - 颜色值必须为有效的Colors枚举或非空字符串
        - 设置后立即生效，影响后续蜡烛图的绘制
        """
        if self._plotinfo.candlestyle:
            return self._plotinfo.candlestyle.bear

    @bear_color.setter
    def bear_color(self, value: str):
        """
        ## 设置下跌蜡烛图（阴线）颜色
        """
        if (value in Colors or (isinstance(value, str) and value)) and self._plotinfo.candlestyle:
            self._light_chart_candles_down_color = True
            self._plotinfo.candlestyle.bear = value

    @property
    def bull_color(self) -> str:
        """
        ## 获取或设置上涨蜡烛图（阳线）颜色

        - 获取当前K线图中上涨蜡烛图（收盘价高于开盘价）的颜色设置
        - 设置K线图中上涨蜡烛图（收盘价高于开盘价）的显示颜色

        Args:
            value (str): 颜色值，支持以下格式：
                - Colors枚举值（如Colors.red）
                - 十六进制颜色字符串（如"#FF0000"）

        Returns:
            str: 上涨蜡烛图的颜色值，格式为十六进制颜色字符串或颜色名称

        Raises:
            ValueError: 当传入无效颜色值时不会生效但不会报错（静默忽略）
            AttributeError: 当蜡烛图样式未启用时访问会引发异常

        ### 示例
        ```python
        # 获取当前上涨蜡烛颜色
        color = kline.bull_color
        print(f"上涨蜡烛颜色: {color}")
        # 设置上涨蜡烛为红色
        kline.bull_color = Colors.red
        # 使用十六进制颜色
        kline.bull_color = "#FF3333"
        ```

        ### Note:
        - 仅当蜡烛图样式启用时（_plotinfo.candlestyle存在）才可访问
        - 默认通常为红色或绿色，取决于图表主题配置
        - 颜色值必须为有效的Colors枚举或非空字符串
        - 设置后立即生效，影响后续蜡烛图的绘制
        """
        if self._plotinfo.candlestyle:
            return self._plotinfo.candlestyle.bull

    @bull_color.setter
    def bull_color(self, value: str):
        """
        ## 设置上涨蜡烛图（阳线）颜色
        """
        if (value in Colors or (isinstance(value, str) and value)) and self._plotinfo.candlestyle:
            self._light_chart_candles_up_color = True
            self._plotinfo.candlestyle.bull = value

    @property
    def datetime_index(self) -> pd.Index:
        """数据时间索引，用于分析报告"""
        return pd.Index(self._dataset.source_object.datetime.values)

    def run(self, *args: tuple[list[str, Multiply]], **kwargs) -> Bt:
        """## 快速启动策略

        ### 📘 **文档参考**:
        - https://www.minibt.cn/minibt_basic/1.4minibt_fast_start_strategy/

        ### Args:
        >>> tuple[list[str, Multiply]]

        ## Kwargs:
        >>> next (Callable)
            config (Config)

        Examples:
        >>> bt= LocalDatas.v2601_300.kline.run(
                ["ma1", Multiply(PandasTa.sma, dict(length=20))],
                ["ma2", Multiply(PandasTa.sma, dict(length=30))],
                ["ma3", Multiply(PandasTa.sma, dict(length=60))],
                ["ma4", Multiply(PandasTa.sma, dict(length=120))],
                ["ebsw", Multiply(PandasTa.ebsw)])

        >>> def next(self: Strategy):
                if not self.kline.position:
                    if self.ma1.prev < self.ma4.prev and self.ma1.new > self.ma4.new:
                        self.kline.buy()
                elif self.kline.position > 0:
                    if self.ma1.prev > self.ma4.prev and self.ma1.new < self.ma4.new:
                        self.kline.sell()
            config = Config(islog=False, profit_plot=True)
            bt= KLine(LocalDatas.get_IndFrame(LocalDatas.v2601_300)).run(
                ["ma1", Multiply(PandasTa.sma, dict(length=20))],
                ["ma2", Multiply(PandasTa.sma, dict(length=30))],
                ["ma3", Multiply(PandasTa.sma, dict(length=60))],
                ["ma4", Multiply(PandasTa.sma, dict(length=120))],
                ["ebsw", Multiply(PandasTa.ebsw)], next=next, config=config)

        Returns:
            Bt
        """
        from ..bt import Bt
        from ..utils import Config as btconfig
        from ..strategy.strategy import Strategy
        df = self
        if args:
            assert all([isinstance(arg, (list, tuple))
                       for arg in args]), "参数必须为list或tuple"
            assert all([len(arg) == 2 for arg in args]
                       ), "参数形式为list[str, Multiply]"
            names = list(map(lambda x: x[0], args))
            assert all([isinstance(name, str) for name in names])
            multiply = list(map(lambda x: x[1], args))
            assert all([isinstance(mult, Multiply) for mult in multiply])
            for m in multiply:
                setattr(m, "data", self)
        next = kwargs.pop("next", None)
        iscallnext = isinstance(next, Callable)
        config = kwargs.pop("config", None)
        config = config if isinstance(config, btconfig) else btconfig(
            islog=False, profit_plot=iscallnext)

        class default_strategy(Strategy):
            def __init__(self) -> None:
                self.data = df  # self.get_kline(df)
                if args:
                    data = self.data.multi_apply(*multiply)
                    for name, ind in zip(names, data):
                        setattr(self, name, ind)
        if iscallnext:
            default_strategy.next = next
        default_strategy.config = config
        if "name" in kwargs:
            default_strategy.__name__ = kwargs.pop("name")

        bt = Bt().addstrategy(default_strategy).run(**kwargs)
        self._strategy_instances.pop(f"{default_strategy.__name__}_0")
        return bt


class IndSeries(IndicatorsBase, PandasSeries, PandasTa, TaLib):
    """## 框架内置指标数据序列类（IndSeries 类型）
    - 核心定位：继承 pandas.Series 并整合指标计算、可视化配置能力，作为系统统一的单列指标数据格式，适用于单一维度的技术指标（如RSI、MACD信号线等）

    ### 📘 **文档参考**:
    - 类简介：https://www.minibt.cn/minibt_basic/1.12minibt_internal_data_series_guide/

    ### 核心特性：
    1. 多父类融合：
    - 继承 `pd.Series`：保留原生 Series 的序列数据存储与计算能力（如索引、切片、元素操作）
    - 继承 `IndicatorsBase`：获得指标基础属性与方法（如指标ID、分类标识）
    - 继承 `PandasTa`/`TaLib`：直接调用 pandas_ta、TaLib 库的技术指标计算接口
    2. 指标化增强：
    - 内置指标元数据管理（`_indsetting` 指标设置、`_plotinfo` 绘图配置）
    - 支持自定义数据标记（`iscustom=True`），适用于用户手动生成的指标序列
    3. 可视化配置集成：
    - 支持线型（`line_style`）、线宽（`line_width`）、颜色（`line_color`）、虚实样式（`line_dash`）的单独设置
    - 自动适配框架绘图模块，无需手动传递绘图参数
    4. 数据兼容性：
    - 支持多种输入数据类型（`pd.Series`/`np.ndarray`/整数），整数输入时自动生成对应长度的全NaN数组
    - 与框架内置 `IndFrame` 类无缝兼容，可通过 `IndSeries` 属性相互转换

    ### 初始化参数说明：
    Args:
        data: 输入数据，支持以下类型：
            - pd.Series：直接使用已有 Series，自动继承其索引与数据
            - np.ndarray：numpy 数组，自动转换为 Series
            - int：整数表示序列长度，自动生成对应长度的全NaN数组（标记为自定义数据）
        **kwargs: 额外配置参数（核心参数如下）：
            - id (BtID)：指标唯一标识（默认自动生成），用于指标区分与管理
            - lines (list[str])：指标线名称列表（默认 ['line']，单列指标通常只需要一个名称）
            - category (str)：指标分类（如 "momentum"/"volatility"，默认 None）
            - isplot (bool)：是否绘图（默认 True）
            - overlap (bool)：是否与主图重叠显示（默认 False，"overlap" 分类指标自动设为 True）
            - sname/ind_name (str)：指标显示名称/内部名称（默认 'name'/'ind_name'）
            - ismain (bool)：是否为主图指标（默认 False）
            - isreplay/isresample (bool)：是否为回放/重采样数据（默认 False）
            - isindicator (bool)：是否为技术指标（默认 True，非指标数据设为 False）
            - iscustom (bool)：是否为自定义数据（默认 False，整数输入时自动设为 True）
            - height (int)：指标绘图高度（默认 150）
            - linestyle/signalstyle/spanstyle：绘图样式配置（默认空字典，使用框架默认样式）

    ### 核心属性说明：
    1. 数据与指标基础属性：
    - _indsetting：`IndSetting` 实例，存储指标元数据（如指标ID、长度、维度标识）
    - _plotinfo：`PlotInfo` 实例，存储绘图配置（如高度、线型、颜色等）
    - _dataset：`DataFrameSet` 实例，管理数据副本与缓存
    - lines：指标线名称列表（通常长度为1，如 ['rsi']）
    2. 样式配置属性（支持直接读写）：
    - line_style：线型完整配置（`LineStyle` 实例，包含线宽、颜色、虚实样式）
    - line_dash：线型虚实样式（如 `LineDash.solid` 实线、`LineDash.dash` 虚线）
    - line_width：线宽（整数或浮点数，如 2 表示2像素宽）
    - line_color：线条颜色（如 `Colors.red` 或 "#FF0000" 十六进制色值）

    ### 使用示例：
    >>> # 1. 从 numpy 数组初始化指标序列
    >>> import numpy as np
    >>> rsi_data = np.array([55.2, 58.7, 62.1, 59.3, 54.8])
    >>> rsi_IndSeries = IndSeries(rsi_data, lines=['rsi'], category='momentum', isplot=True)
    >>>
    >>> # 2. 自定义线型样式
    >>> rsi_IndSeries.line_color = Colors.blue  # 设置为蓝色
    >>> rsi_IndSeries.line_width = 2            # 线宽设为2
    >>> rsi_IndSeries.line_dash = LineDash.dash # 虚线样式
    >>>
    >>> # 3. 从整数长度初始化（自定义数据）
    # 生成100长度的全NaN序列
    >>> ma_IndSeries = IndSeries(100, iscustom=True, lines=['ma20'])
    >>>
    >>> # 4. 调用 pandas_ta 指标（继承自 PandasTa）
    >>> close_IndSeries = IndSeries(close_prices)  # close_prices 为价格序列
    >>> macd_signal = close_IndSeries.macd().signal  # 获取MACD信号线（Line类型）
    """

    def __init__(self, data: pd.Series | np.ndarray | int | None = None, **kwargs: IndSetting | dict) -> None:
        if data is None:
            data = np.full(0, np.nan)
            kwargs.update(dict(iscustom=True))
        if isinstance(data, int):
            assert data > 0, "整数类必须大于0"
            data = np.full(data, np.nan)
            kwargs.update(dict(iscustom=True))
        if hasattr(data, "pandas_object"):
            data = data.pandas_object
        super().__init__(data)
        btid = kwargs.pop("id", BtID())
        if isinstance(btid, dict):
            btid = BtID(**btid)
        if not isinstance(btid, BtID):
            btid = BtID()
        lines = Lines(*kwargs.pop('lines', ['line',]))
        lines = Lines(*lines)(self)
        category = kwargs.pop('category', Category.Any)
        if not isinstance(category, str) or not category:
            category = Category.Any
        if not isinstance(category, CategoryString):
            category = CategoryString(category)
        isplot = kwargs.pop('isplot', True)
        overlap = kwargs.pop('overlap', False)
        overlap = category == "overlap" and True or bool(overlap)
        sname = kwargs.pop('sname', 'name')
        ind_name = kwargs.pop('ind_name', sname)
        ismain = bool(kwargs.pop('ismain', False))
        isreplay = bool(kwargs.pop('isreplay', False))
        isresample = bool(kwargs.pop('isresample', False))
        isindicator = True
        iscustom = bool(kwargs.pop('iscustom', False))
        is_mir = kwargs.pop('_is_mir', False)
        isMDim = False
        dim_match = kwargs.pop("dim_match", True)
        height = kwargs.pop("height", 150)
        linestyle = kwargs.pop("linestyle", {})
        signalstyle = kwargs.pop("signalstyle", Addict())  # AutoNameDict())
        span = kwargs.pop("spanstyle", np.nan)
        self._indsetting = IndSetting(
            btid,
            is_mir,
            False,
            False,
            ismain,
            isreplay,
            isresample,
            isindicator,
            iscustom,
            isMDim,
            dim_match,
        )
        plotinfo = kwargs.pop('plotinfo', None)
        if not isinstance(plotinfo, PlotInfo):
            plotinfo = PlotInfo(
                height=height,
                sname=sname,
                ind_name=ind_name,
                lines=lines,
                line_filed=[],
                category=category,
                isplot=isplot,
                overlap=overlap,
                linestyle=linestyle,
                signalstyle=signalstyle,
                spanstyle=span,
            )
        self._plotinfo = plotinfo
        kline = kwargs.pop("kline", None)
        if kline is not None and hasattr(kline, "_indsetting"):
            self._indsetting.id = kline._indsetting.id.copy()
        self._dataset = DataFrameSet(
            self.copy(),
            kline_object=kline,
            source_object=kwargs.pop("source", None),
            copy_object=self.copy())
        if self._indsetting.iscustom:
            self._dataset.custom_object = data
        self.cache = Cache(maxsize=np.inf)

    @property
    def line_style(self) -> LineStyle:
        """
        ## 获取或设置指标线的完整样式配置

        - 获取或设置当前指标序列的完整线型样式，包括线型、宽度和颜色
        - 设置当前指标序列的完整线型样式，一次性配置线型、宽度和颜色

        Args:
            value (LineStyle): 线型样式对象，包含line_dash、line_width、line_color属性

        Returns:
            LineStyle: 当前指标线的完整样式对象

        ### 示例
        ```python
        # 获取当前指标线的完整样式
        style = ind_series.line_style
        print(f"线型: {style.line_dash}, 宽度: {style.line_width}, 颜色: {style.line_color}")
        # 设置指标线的完整样式
        ind_series.line_style = LineStyle(
            line_dash=LineDash.dashdot,
            line_width=2.5,
            line_color=Colors.blue
        )
        ```

        ### GetNote:
        - 返回的是LineStyle对象的引用，修改返回的对象会影响原始样式
        - 仅适用于单线指标序列

        ### SetNote:
        - 必须传入LineStyle类型的实例
        - 设置后会立即覆盖原有的所有线型属性
        - 仅适用于单线指标序列
        """
        return self._plotinfo.linestyle[self.lines[0]]

    @line_style.setter
    def line_style(self, value: LineStyle):
        """
        ## 设置指标线的完整样式"""
        if isinstance(value, LineStyle):
            self._plotinfo.linestyle[self.lines[0]] = value

    @property
    def line_dash(self) -> str | LineDash:
        """
        ## 获取或设置指标线的虚线样式

        - 获取或设置当前指标序列的虚线样式（如实线、虚线、点线等）
        - 设置当前指标序列的虚线样式，改变线条的绘制方式

        Args:
            value (str | LineDash): 虚线样式，必须是LineDash枚举值

        Returns:
            str | LineDash: 当前指标线的虚线样式

        ### 示例
        ```python
        # 获取当前线型
        dash_style = ind_series.line_dash
        print(f"当前线型: {dash_style}")

        # 设置指标线为虚线
        ind_series.line_dash = LineDash.dash

        # 设置指标线为点划线
        ind_series.line_dash = LineDash.dashdot
        ```

        ### GetNote:
        - 返回值为LineDash枚举值或对应的字符串
        - 仅适用于单线指标序列（lines[0]）
        - 可用值包括：LineDash.solid(实线)、LineDash.dash(虚线)、
        - LineDash.dot(点线)、LineDash.dashdot(点划线)

        ### SetNote:
        - 仅当传入值为有效的LineDash枚举值时设置才会生效
        - 仅适用于单线指标序列（lines[0]）
        - 设置后立即生效，影响图表显示
        """
        return self._plotinfo.linestyle[self.lines[0]].line_dash

    @line_dash.setter
    def line_dash(self, value: str | LineDash):
        """
        ## 设置指标线的虚线样式
        """
        if value in LineDash:
            self._plotinfo.linestyle[self.lines[0]].line_dash = value

    @property
    def line_width(self) -> int | float:
        """
        ## 获取或设置指标线的宽度

        - 获取或设置当前指标序列的线条宽度（以像素为单位）
        - 设置当前指标序列的线条宽度，控制线条在图表上的粗细

        Args:
            value (int | float): 线条宽度值，必须为数值类型

        Returns:
            int | float: 当前指标线的宽度值
        ### 示例
        ```python
        # Example: 获取当前线宽
        width = ind_series.line_width
        print(f"当前线宽: {width}像素")

        # Example: 设置指标线宽度为2像素
        ind_series.line_width = 2

        # Example: 设置指标线宽度为1.5像素
        ind_series.line_width = 1.5
        ```

        ### GetNote:
        - 返回值为数值类型，表示像素宽度
        - 默认值通常为1.3
        - 仅适用于单线指标序列

        ### SetNote:
        - 仅当传入值为数值类型时设置才会生效
        - 仅适用于单线指标序列
        - 建议值范围：0.5-5.0，过大可能影响图表美观
        """
        return self._plotinfo.linestyle[self.lines[0]].line_width

    @line_width.setter
    def line_width(self, value: int | float):
        """
        ## 设置指标线的宽度
        """
        if isinstance(value, (int, float)):
            self._plotinfo.linestyle[self.lines[0]].line_width = value

    @property
    def line_color(self) -> str | Colors:
        """
        ## 获取或设置指标线的颜色

        - 获取或设置当前指标序列的线条颜色
        - 设置当前指标序列的线条颜色，改变线条在图表上的显示颜色

        Args:
            value (str | Colors): 颜色值，可以是Colors枚举或字符串格式

        Returns:
            str | Colors: 当前指标线的颜色值
        ### 示例
        ```python
        # 获取当前线色
        color = ind_series.line_color
        print(f"当前线色: {color}")

        # 使用Colors枚举设置颜色
        ind_series.line_color = Colors.red

        # 使用十六进制字符串设置颜色
        ind_series.line_color = "#00FF00"
        ```
        ### GetNote:
        - 返回值为Colors枚举值或颜色字符串
        - 仅适用于单线指标序列
        - 颜色值可以是十六进制字符串（如"#FF0000"）或Colors枚举（如Colors.red）

        ### SetNote:
        - 颜色值必须是有效的Colors枚举或非空字符串
        - 仅适用于单线指标序列
        - 建议选择与背景对比明显的颜色以提高可读性
        """
        return self._plotinfo.linestyle[self.lines[0]].line_color

    @line_color.setter
    def line_color(self, value: str | Colors):
        """
        ## 设置指标线的颜色
        """
        if isinstance(value, str) or value in Colors:
            self._plotinfo.linestyle[self.lines[0]].line_color = value

    @property
    def price_line(self) -> str | Colors:
        """
        ## 指标线价格水平线显示配置

        - 只适配lightweight_charts图表
        - 用于配置指标线/直方图是否显示价格水平线（在图表中指标价格水平线）
        - 支持两种使用方式，适配不同的配置场景：

        1. **链式调用**：为特定指标线单独设置价格标签显示状态
        2. **统一设置**：为当前指标实例下所有线统一设置价格标签显示状态

        Returns:
            LineAttrType: 线型属性配置对象，支持链式调用设置单个属性值

        ```python
        # 链式调用：为long_signal指标线开启价格水平线显示
        indicator.price_line.long_signal = True

        # 统一设置：为当前指标所有线关闭价格水平线显示
        indicator.price_line = False
        """
        return self._plotinfo.linestyle[self.lines[0]].price_line

    @price_line.setter
    def price_line(self, value):
        self._plotinfo.linestyle[self.lines[0]].price_line = bool(value)

    @property
    def price_label(self) -> str | Colors:
        """
        ## 指标线价格标签显示配置

        - 只适配lightweight_charts图表
        - 用于配置指标线/直方图是否显示价格标签（在图表中指标数值旁显示价格文本）
        - 支持两种使用方式，适配不同的配置场景：

        1. **链式调用**：为特定指标线单独设置价格标签显示状态
        2. **统一设置**：为当前指标实例下所有线统一设置价格标签显示状态

        Returns:
            LineAttrType: 线型属性配置对象，支持链式调用设置单个属性值

        ```python
        # 链式调用：为long_signal指标线开启价格标签显示
        indicator.price_label.long_signal = True

        # 统一设置：为当前指标所有线关闭价格标签显示
        indicator.price_label = False
        """
        return self._plotinfo.linestyle[self.lines[0]].price_label

    @price_label.setter
    def price_label(self, value):
        """
        ## 设置指标线的颜色
        """
        self._plotinfo.linestyle[self.lines[0]].price_label = bool(value)


class Line(IndSeries):
    """## 框架内置单列指标数据类（继承 IndSeries 类）
    - 核心定位：作为 `IndFrame`/`KLine` 类的「列级数据载体」，封装单列指标的完整能力，同时保持与源数据（父 `IndFrame`/`KLine`）的联动，实现样式配置、绘图状态的双向同步

    ### 📘 **文档参考**:
    - 类简介：https://www.minibt.cn/minibt_basic/1.13minibt_internal_data_line_guide/

    ### 核心特性：
    1. 源数据强关联：
    - 初始化时必须绑定父级 `IndFrame` 或 `KLine` 实例（`source` 参数），确保与源数据共享元信息（如指标分类、绘图配置）
    - 样式修改（如颜色、线型）会自动同步到父级源数据，避免父子数据配置不一致
    2. 信号与普通指标双适配：
    - 自动识别是否为交易信号线（通过 `sname` 是否在父级 `signallines` 中判断）
    - 信号线额外支持信号样式配置（标记 `signal_marker`、颜色 `signal_color` 等），普通指标线仅保留基础线型配置
    3. 样式配置双向同步：
    - 线型（`line_style`）、线宽（`line_width`）、颜色（`line_color`）等基础样式修改时，同时更新自身与父级源数据的绘图配置
    - 信号专属样式（`signal_style`/`signal_key` 等）修改时，自动同步父级源数据的信号配置，确保绘图时信号与源数据匹配
    4. 继承与扩展并重：
    - 完全继承 `IndSeries` 类的指标计算能力（如调用 `pandas_ta`/`TaLib` 接口）、数据操作能力（索引、切片等）
    - 新增与父级联动的属性（如 `follow` 继承父级跟随状态、`overlap` 同步父级重叠显示配置），强化列级数据的上下文关联性

    ### 初始化参数说明：
    Args:
        source (IndFrame | KLine): 父级源数据实例，必须为框架内置的多列数据类型（`IndFrame` 或 `KLine`），用于关联元信息与同步配置
        data: 单列指标数据，支持类型与 `IndSeries` 类一致（`pd.Series`/`np.ndarray`/整数，整数表示生成对应长度的全NaN数组）
        **kwargs (IndSetting | dict): 额外配置参数（核心参数继承自 `IndSeries` 类，新增/增强如下）：
                - sname (str): 指标线名称（必须与父级 `IndFrame`/`KLine` 的列名一致，用于匹配父级配置）
                - 其他参数（如 `category`/`isplot`/`overlap` 等）若未指定，默认继承自父级源数据

    ### 核心属性说明：
    1. 源数据关联属性：
    - source: 父级源数据实例（`IndFrame` 或 `KLine`），可直接通过该属性访问父级的完整能力（如其他列数据、合约信息等）
    - follow: 继承父级的「主图跟随状态」（仅 `KLine` 等K线类数据有效，默认与父级一致）
    - isplot: 绘图开关，修改时自动同步父级源数据中对应列的绘图状态（非蜡烛图类父级有效）
    - overlap: 与主图重叠显示开关，修改时同步父级源数据对应列的重叠配置（非蜡烛图类父级有效）
    2. 基础线型配置属性（双向同步父级）：
    - line_style: 完整线型配置（`LineStyle` 实例），修改时同步父级源数据对应列的线型
    - line_dash: 线型虚实样式（如 `LineDash.solid` 实线），修改时同步父级
    - line_width: 线宽（整数/浮点数，需大于0），修改时同步父级
    - line_color: 线条颜色（支持 `Colors` 枚举或十六进制字符串），修改时同步父级
    3. 信号专属配置属性（仅信号线有效，双向同步父级）：
    - signal_style: 信号完整样式（`SignalStyle` 实例），仅当该线为信号线时可访问
    - signal_key: 信号锚定的父级列名（如信号标记在 "low" 列价格位置），修改时同步父级
    - signal_marker: 信号标记样式（如 `Markers.circle_dot` 圆点），修改时同步父级
    - signal_color: 信号颜色，修改时同步父级
    - signal_size: 信号大小，修改时同步父级
    - signal_show: 信号显示开关，修改时同步父级

    ### 使用示例：
    >>> # 1. 从 KLine 父级数据创建 Line 实例（假设 kline 为 KLine 实例，含 "close" 列）
    >>> close_line = self.data.close
    >>>
    >>> # 2. 修改线型样式（自动同步到父级 KLine）
    >>> close_line.line_color = Colors.red  # 关闭线设为红色，父级 kline 的 "close" 列颜色同步更新
    >>> close_line.line_width = 2           # 线宽设为2，父级同步更新
    >>>
    >>> # 3. 信号线配置（假设 "long_signal" 是父级的信号列）
    >>> if long_line.issignal:  # 自动识别为信号线
    ...     long_line.signal_marker = Markers.triangle_up  # 信号标记设为上三角
    ...     long_line.signal_color = Colors.green          # 信号颜色设为绿色，父级同步更新
    >>>
    >>> # 4. 控制绘图状态（同步父级）
    >>> long_line.isplot = True  # 开启信号线绘图，父级 kline 的 "long_signal" 列绘图状态同步开启"""

    def __init__(self, source: IndFrame | KLine, data, **kwargs: IndSetting | dict) -> None:
        super().__init__(data, **kwargs)
        self.__source = source  # IndFrame数据
        self._issignal: bool = self.sname in source.signallines
        self._dataset.source_object = source

    @property
    def line_style(self) -> LineStyle | None:
        """
        ## 完整线型配置（LineStyle 实例）

        - Get：
            - 获取当前列的完整线型配置（包含线宽、颜色、虚实样式等），
            - 从自身 _plotinfo.linestyle 中取对应 sname 的配置；
        - Set：
            - 设置当前列的完整线型配置，参数必须为 LineStyle 实例；
            - 设置时会先更新自身 _plotinfo.linestyle（用 Addict 包装确保格式一致），
            - 再同步更新父级 source 的 _plotinfo.linestyle，
            - 保证父子数据线型配置完全一致。

        Args:
            value (LineStyle): 线型样式对象，包含line_dash、line_width、line_color属性

        Returns:
            LineStyle | None: 当前列的完整线型配置对象，如果未设置则返回None

        ### 示例
        ```python
        # 获取当前列的线型样式
        style = line_obj.line_style
        print(f"线型: {style.line_dash}, 宽度: {style.line_width}, 颜色: {style.line_color}")

        # 设置当前列的完整线型样式
        line_obj.line_style = LineStyle(
            line_dash=LineDash.dash,
            line_width=2.0,
            line_color=Colors.blue
        )
        ```

        ### GetNote:
            - 返回的是LineStyle对象的引用，修改返回的对象会影响原始样式
            - 如果该列尚未设置线型样式，可能返回None

        ### SetNote:
            - 必须传入LineStyle类型的实例
            - 设置后会立即同步到父级数据源，确保显示一致性
            - 使用Addict包装确保数据结构一致性
        """
        return self._plotinfo.linestyle.get(self.sname)

    @line_style.setter
    def line_style(self, value):
        """
        ## 设置完整线型配置
        """
        if isinstance(value, LineStyle):
            self._plotinfo.linestyle = Addict({self.sname: value})
            self.__source._plotinfo.linestyle.update(
                {self.sname: value})

    @property
    def line_dash(self) -> LineDash:
        """
        ## 线型虚实样式（LineDash 枚举值）

        - Get：
            - 获取当前列的线型虚实样式，
            - 从自身 _plotinfo.linestyle 中对应 sname 的 line_dash 字段取值；
        - Set：
            - 设置当前列的线型虚实样式，
            - 参数必须为 LineDash 枚举成员（如 LineDash.solid 实线）；
            - 设置时先更新自身配置，再同步父级 source 的 _plotinfo.linestyle 中对应 sname 的 line_dash，确保线型统一。

        Args:
            value (LineDash): 虚线样式，必须是LineDash枚举值

        Returns:
            LineDash: 当前列的线型虚实样式

        ### 示例
        ```python
        # 获取当前线型虚实样式
        dash_style = line_obj.line_dash
        print(f"当前线型: {dash_style}")

        # 设置线型为实线
        line_obj.line_dash = LineDash.solid

        # 设置线型为点划线
        line_obj.line_dash = LineDash.dashdot
        ```

        ### GetNote:
            - 返回值为LineDash枚举成员
            - 如果未设置，将返回默认值

        ### SetNote:
            - 参数必须是LineDash枚举成员
            - 设置后会同步到父级数据源，确保显示一致性
            - 可用值包括：solid(实线)、dash(虚线)、dot(点线)、dashdot(点划线)
        """
        return self._plotinfo.linestyle[self.sname].line_dash

    @line_dash.setter
    def line_dash(self, value):
        """
        ## 设置线型虚实样式
        """
        if value in LineDash:
            self._plotinfo.linestyle[self.sname].line_dash = value
            self.__source._plotinfo.linestyle[self.sname].line_dash = value

    @property
    def line_width(self) -> float:
        """
        ## 线条宽度

        - Get：
            - 获取当前列的线条宽度，
            - 从自身 _plotinfo.linestyle 中对应 sname 的 line_width 字段取值；
        - Set：
            - 设置当前列的线条宽度，
            - 参数需为正数（int/float），且会自动转为 float 类型；
            - 设置时先更新自身配置，再同步父级 source 的 _plotinfo.linestyle 中对应 sname 的 line_width，避免线宽不一致。

        Args:
            value (int | float): 线条宽度值，必须为大于0的数值

        Returns:
            float: 当前列的线条宽度（像素）

        ### 示例
        ```python
        # 获取当前线条宽度
        width = line_obj.line_width
        print(f"当前线宽: {width}像素")

        # 设置线条宽度为2像素
        line_obj.line_width = 2

        # 设置线条宽度为1.5像素
        line_obj.line_width = 1.5
        ```

        ### GetNote:
            - 返回值为浮点数，表示像素宽度
            - 如果未设置，将返回默认值

        ### SetNote:
            - 参数必须为大于0的数值
            - 会自动转换为float类型存储
            - 设置后会同步到父级数据源，确保显示一致性
            - 建议值范围：0.5-5.0，过大可能影响图表美观
        """
        return self._plotinfo.linestyle[self.sname].line_width

    @line_width.setter
    def line_width(self, value):
        """
        ## 设置线条宽度
        """
        if isinstance(value, (float, int)) and value > 0.:
            value = float(value)
            self._plotinfo.linestyle[self.sname].line_width = value
            self.__source._plotinfo.linestyle[self.sname].line_width = value

    @property
    def line_color(self) -> Colors | str:
        """
        ## 线条颜色

        - Get：
            - 获取当前列的线条颜色，
            - 从自身 _plotinfo.linestyle 中对应 sname 的 line_color 字段取值；
        - Set：
            - 设置当前列的线条颜色，
            - 参数支持 Colors 枚举成员（如 Colors.RED）或合法十六进制字符串（如 "#FF0000"）；
            - 设置时先更新自身配置，再同步父级 source 的 _plotinfo.linestyle 中对应 sname 的 line_color，确保颜色统一。

        Args:
            value (Colors | str): 颜色值，支持Colors枚举成员或合法的十六进制颜色字符串

        Returns:
            Colors | str: 当前列的线条颜色

        ### 示例
        ```python
        # 获取当前线条颜色
        color = line_obj.line_color
        print(f"当前线条颜色: {color}")

        # 使用Colors枚举设置颜色
        line_obj.line_color = Colors.red

        # 使用十六进制字符串设置颜色
        line_obj.line_color = "#00FF00"

        # 使用颜色名称设置颜色
        line_obj.line_color = "blue"
        ```

        ### GetNote:
            - 返回值为Colors枚举成员或十六进制颜色字符串
            - 如果未设置，将返回默认值

        ### SetNote:
            - 参数必须是有效的Colors枚举成员或非空颜色字符串
            - 颜色字符串必须是合法的十六进制格式（如"#FF0000"）
            - 设置后会同步到父级数据源，确保显示一致性
            - 建议选择与背景对比明显的颜色以提高可读性
        """
        return self._plotinfo.linestyle[self.sname].line_color

    @line_color.setter
    def line_color(self, value):
        """
        ## 设置线条颜色
        """
        if value in Colors or (value and isinstance(value, str)):
            self._plotinfo.linestyle[self.sname].line_color = value
            self.__source._plotinfo.linestyle[self.sname].line_color = value

    @property
    def price_line(self) -> str | Colors:
        """
        ## 指标线价格水平线显示配置

        - 只适配lightweight_charts图表
        - 用于配置指标线/直方图是否显示价格水平线（在图表中指标价格水平线）
        - 支持两种使用方式，适配不同的配置场景：

        1. **链式调用**：为特定指标线单独设置价格标签显示状态
        2. **统一设置**：为当前指标实例下所有线统一设置价格标签显示状态

        Returns:
            LineAttrType: 线型属性配置对象，支持链式调用设置单个属性值

        ```python
        # 链式调用：为long_signal指标线开启价格水平线显示
        indicator.price_line.long_signal = True

        # 统一设置：为当前指标所有线关闭价格水平线显示
        indicator.price_line = False
        """
        return self._plotinfo.linestyle[self.sname].price_line

    @price_line.setter
    def price_line(self, value):
        self._plotinfo.linestyle[self.sname].price_line = bool(value)
        self.__source._plotinfo.linestyle[self.sname].price_line = value

    @property
    def price_label(self) -> str | Colors:
        """
        ## 指标线价格标签显示配置

        - 只适配lightweight_charts图表
        - 用于配置指标线/直方图是否显示价格标签（在图表中指标数值旁显示价格文本）
        - 支持两种使用方式，适配不同的配置场景：

        1. **链式调用**：为特定指标线单独设置价格标签显示状态
        2. **统一设置**：为当前指标实例下所有线统一设置价格标签显示状态

        Returns:
            LineAttrType: 线型属性配置对象，支持链式调用设置单个属性值

        ```python
        # 链式调用：为long_signal指标线开启价格标签显示
        indicator.price_label.long_signal = True

        # 统一设置：为当前指标所有线关闭价格标签显示
        indicator.price_label = False
        """
        return self._plotinfo.linestyle[self.sname].price_label

    @price_label.setter
    def price_label(self, value):
        self._plotinfo.linestyle[self.sname].price_label = bool(value)
        self.__source._plotinfo.linestyle[self.sname].price_label = value

    @property
    def signal_style(self) -> SignalStyle | None:
        """
        ## 信号完整样式配置（仅信号线有效）

        - Get：
            - 获取当前信号线的完整样式配置（SignalStyle 实例），
            - 仅当 _issignal 为 True（是信号线）时有效，否则返回 None；
            - 从自身 _plotinfo.signalstyle 中对应 sname 的配置取值；
        - Set：
            - 设置当前信号线的完整样式配置，
            - 参数必须为 SignalStyle 实例，且仅当 _issignal 为 True 时生效；
            - 设置时先更新父级 source 的 _plotinfo.signalstyle 中对应 sname 的配置，再同步自身配置，确保信号样式一致。

        Args:
            value (SignalStyle): 信号样式对象，包含key、color、marker、show、size、label等属性

        Returns:
            SignalLabel | None: 当前信号线的完整样式配置对象，如果非信号线则返回None

        ### 示例
        ```python
        # 获取信号线完整样式
        signal_style = line_obj.signal_style
        if signal_style:
            print(f"信号颜色: {signal_style.color}, 标记: {signal_style.marker}")

        # 设置信号线完整样式
        line_obj.signal_style = SignalStyle(
            key="close",
            color=Colors.green,
            marker=Markers.circle,
            show=True,
            size=10
        )
        ```

        ### GetNote:
            - 仅对信号线有效，非信号线返回None
            - 返回SignalStyle对象引用，修改会影响原始配置

        ### SetNote:
            - 必须传入SignalStyle类型的实例
            - 仅对信号线有效，非信号线设置无效
            - 设置后会同步到父级数据源，确保显示一致性
            - SignalLabel属性可配置：text(文字)、size(大小)、style(样式)、color(颜色)
        """
        if self._issignal:
            return self._plotinfo.signalstyle[self.sname]

    @signal_style.setter
    def signal_style(self, value):
        """
        ## 设置信号完整样式配置
        """
        if self._issignal and isinstance(value, SignalStyle):
            self.__source._plotinfo.signalstyle[self.sname] = value
            self._plotinfo.signalstyle[self.sname] = value

    @property
    def signal_key(self) -> str | None:
        """
        ## 信号锚定的父级列名（仅信号线有效）

        - Get：
            - 获取当前信号线锚定的父级列名（如 "low" 表示信号标记在父级 low 列价格位置），
            - 仅当 _issignal 为 True 时有效；
            - 从自身 _plotinfo.signalstyle 中对应 sname 的 key 字段取值；
        - Set：
            - 设置当前信号线锚定的父级列名，参数必须是父级 source.lines 中的有效列名（确保锚定列存在），
            - 且仅当 _issignal 为 True 时生效；
            - 设置时同步更新父级 source 和自身的 _plotinfo.signalstyle 中对应 sname 的 key，确保锚定列一致。

        Args:
            value (str): 父级列名，必须是source.lines中的有效列名

        Returns:
            str | None: 当前信号线锚定的父级列名，如果非信号线则返回None

        ### 示例
        ```python
        # 获取信号锚定列名
        key = line_obj.signal_key
        print(f"信号锚定在: {key}")

        # 设置信号锚定在最低价
        line_obj.signal_key = "low"

        # 设置信号锚定在收盘价
        line_obj.signal_key = "close"
        ```

        ### GetNote:
            - 仅对信号线有效，非信号线返回None
            - 返回值为字符串，表示锚定的父级数据列名

        ### SetNote:
            - 参数必须是父级source.lines中的有效列名
            - 仅对信号线有效，非信号线设置无效
            - 设置后会同步到父级数据源，确保锚定位置一致
        """
        if self._issignal:
            return self._plotinfo.signalstyle[self.sname].key

    @signal_key.setter
    def signal_key(self, value) -> None:
        """
        ## 设置信号锚定列名
        """
        if self._issignal:
            if value in self.__source.lines:
                self.__source._plotinfo.signalstyle[self.sname].key = value
                self._plotinfo.signalstyle[self.sname].key = value

    @property
    def signal_color(self) -> str | None:
        """
        ## 信号标记颜色（仅信号线有效）

        - Get：
            - 获取当前信号线的标记颜色，仅当 _issignal 为 True 时有效；
            - 从自身 _plotinfo.signalstyle 中对应 sname 的 color 字段取值；
        - Set：
            - 设置当前信号线的标记颜色，参数必须为 Colors 枚举成员，且仅当 _issignal 为 True 时生效；
            - 设置时同步更新父级 source 和自身的 _plotinfo.signalstyle 中对应 sname 的 color，确保信号颜色一致。

        Args:
            value (Colors): 颜色值，必须是Colors枚举成员

        Returns:
            str | None: 当前信号线的标记颜色，如果非信号线则返回None

        ### 示例
        ```python
        # 获取信号颜色
        color = line_obj.signal_color
        print(f"信号颜色: {color}")

        # 设置信号为红色
        line_obj.signal_color = Colors.red

        # 设置信号为绿色
        line_obj.signal_color = Colors.green
        ```

        ### GetNote:
            - 仅对信号线有效，非信号线返回None
            - 返回值为Colors枚举成员对应的颜色值

        ### SetNote:
            - 参数必须是有效的Colors枚举成员
            - 仅对信号线有效，非信号线设置无效
            - 设置后会同步到父级数据源，确保颜色显示一致
            - 常用颜色：red(红)、green(绿)、blue(蓝)、orange(橙)
        """
        if self._issignal:
            return self._plotinfo.signalstyle[self.sname].color

    @signal_color.setter
    def signal_color(self, value) -> None:
        """
        ## 设置信号标记颜色
        """
        if self._issignal:
            if value in Colors:
                self.__source._plotinfo.signalstyle[self.sname].color = value
                self._plotinfo.signalstyle[self.sname].color = value

    @property
    def signal_marker(self) -> str | None:
        """
        ## 信号标记样式（仅信号线有效）

        - Get：
            - 获取当前信号线的标记样式（如 "circle" 圆形、"triangle_up" 上三角），
            - 仅当 _issignal 为 True 时有效；
            - 从自身 _plotinfo.signalstyle 中对应 sname 的 marker 字段取值；
        - Set：
            - 设置当前信号线的标记样式，参数必须为 Markers 枚举成员，且仅当 _issignal 为 True 时生效；
            - 设置时同步更新父级 source 和自身的 _plotinfo.signalstyle 中对应 sname 的 marker，确保标记样式一致。

        Args:
            value (Markers): 标记样式，必须是Markers枚举成员

        Returns:
            str | None: 当前信号线的标记样式，如果非信号线则返回None

        ### 示例
        ```python
        # 获取信号标记样式
        marker = line_obj.signal_marker
        print(f"信号标记: {marker}")

        # 设置信号为圆形标记
        line_obj.signal_marker = Markers.circle

        # 设置信号为上三角形标记
        line_obj.signal_marker = Markers.triangle_up

        # 设置信号为星形标记
        line_obj.signal_marker = Markers.star
        ```

        ### GetNote:
            - 仅对信号线有效，非信号线返回None
            - 返回值为Markers枚举成员对应的标记样式

        ### SetNote:
            - 参数必须是有效的Markers枚举成员
            - 仅对信号线有效，非信号线设置无效
            - 设置后会同步到父级数据源，确保标记样式一致
            - 常用标记：circle(圆形)、triangle_up(上三角)、triangle_down(下三角)、star(星形)
        """
        if self._issignal:
            return self._plotinfo.signalstyle[self.sname].marker

    @signal_marker.setter
    def signal_marker(self, value) -> None:
        """
        ## 设置信号标记样式
        """
        if self._issignal:
            if value in Markers:
                self.__source._plotinfo.signalstyle[self.sname].marker = value
                self._plotinfo.signalstyle[self.sname].marker = value

    @property
    def signal_show(self) -> bool | None:
        """
        ## 信号显示开关（仅信号线有效）

        - Get：
            - 获取当前信号线的显示状态（True 显示、False 隐藏），仅当 _issignal 为 True 时有效；
            - 从自身 _plotinfo.signalstyle 中对应 sname 的 show 字段取值；
        - Set：
            - 设置当前信号线的显示状态，参数会自动转为 bool 类型，且仅当 _issignal 为 True 时生效；
            - 设置时同步更新父级 source 和自身的 _plotinfo.signalstyle 中对应 sname 的 show，确保显示状态一致。

        Args:
            value (Any): 显示状态，可以是任何可转换为布尔值的类型

        Returns:
            bool | None: 当前信号线的显示状态，如果非信号线则返回None

        ### 示例
        ```python
        # 获取信号显示状态
        is_show = line_obj.signal_show
        print(f"信号显示: {'是' if is_show else '否'}")

        # 显示信号
        line_obj.signal_show = True

        # 隐藏信号
        line_obj.signal_show = False

        # 使用数值控制显示
        line_obj.signal_show = 1  # 等同于True
        line_obj.signal_show = 0  # 等同于False
        ```

        ### GetNote:
            - 仅对信号线有效，非信号线返回None
            - 返回值为布尔类型，True表示显示，False表示隐藏

        ### SetNote:
            - 参数会自动转换为布尔类型
            - 仅对信号线有效，非信号线设置无效
            - 设置后会同步到父级数据源，确保显示状态一致
            - 当值为0、空字符串等假值时转换为False，否则转换为True
        """
        if self._issignal:
            return self._plotinfo.signalstyle[self.sname].show

    @signal_show.setter
    def signal_show(self, value) -> None:
        """
        ## 设置信号显示开关
        """
        if self._issignal:
            value = bool(value)
            self.__source._plotinfo.signalstyle[self.sname].show = value
            self._plotinfo.signalstyle[self.sname].show = value

    @property
    def signal_size(self) -> float | None:
        """
        ## 信号标记大小（仅信号线有效）

        - Get：
            - 获取当前信号线的标记大小，仅当 _issignal 为 True 时有效；
            - 从自身 _plotinfo.signalstyle 中对应 sname 的 size 字段取值；
        - Set：
            - 设置当前信号线的标记大小，参数需为正数（int/float），且仅当 _issignal 为 True 时生效；
            - 设置时同步更新父级 source 和自身的 _plotinfo.signalstyle 中对应 sname 的 size，确保标记大小一致。

        Args:
            value (int | float): 标记大小，必须是大于0的数值

        Returns:
            float | None: 当前信号线的标记大小，如果非信号线则返回None

        ### 示例
        ```python
        # 获取信号标记大小
        size = line_obj.signal_size
        print(f"信号标记大小: {size}")

        # 设置信号标记大小为8
        line_obj.signal_size = 8

        # 设置信号标记大小为12.5
        line_obj.signal_size = 12.5
        ```

        ### GetNote:
            - 仅对信号线有效，非信号线返回None
            - 返回值为浮点数，表示标记大小

        ### SetNote:
            - 参数必须是大于0的数值
            - 仅对信号线有效，非信号线设置无效
            - 设置后会同步到父级数据源，确保标记大小一致
            - 建议值范围：5-20，过小不易识别，过大会影响图表美观
        """
        if self._issignal:
            return self._plotinfo.signalstyle[self.sname].size

    @signal_size.setter
    def signal_size(self, value) -> None:
        """
        ## 设置信号标记大小
        """
        if self._issignal:
            if isinstance(value, (int, float)) and value > 0:
                self.__source._plotinfo.signalstyle[self.sname].size = value
                self._plotinfo.signalstyle[self.sname].size = value

    @property
    def signal_label(self) -> SignalLabel | bool | None:
        """
        ## 信号标签配置（仅信号线有效）

        - Get：
            - 获取当前信号线的标签配置（SignalLabel 实例，含文字、偏移、字体样式等），
            - 仅当 _issignal 为 True 时有效；
            - 从自身 _plotinfo.signalstyle 中对应 sname 的 label 字段取值；
        - Set：
            - 设置当前信号线的标签配置，参数可以是SignalLabel实例、字符串或布尔值，
            - 且仅当 _issignal 为 True 时生效；
            - 设置时同步更新父级 source 和自身的 _plotinfo.signalstyle 中对应 sname 的 label，确保标签配置一致。

        Args:
            value (SignalLabel | str | bool): 标签配置，可以是：
                - SignalLabel实例：完整标签配置
                - 字符串：标签文字内容
                - 布尔值：True启用默认标签，False禁用标签

        Returns:
        >>> SignalLabel | bool | None: 当前信号线的标签配置，可能是SignalLabel实例或布尔值

        ### 示例
        ```python
        # 获取信号标签配置
        label = line_obj.signal_label
        if isinstance(label, SignalLabel):
            print(f"标签文字: {label.text}, 大小: {label.size}")

        # 使用SignalLabel实例设置完整标签
        line_obj.signal_label = SignalLabel(
            text="买入信号",
            size=12,
            style="bold",
            color=Colors.red,
        )

        # 使用字符串设置标签文字
        line_obj.signal_label = "卖出信号"

        # 启用默认标签
        line_obj.signal_label = True

        # 禁用标签
        line_obj.signal_label = False
        ```

        ### GetNote:
            - 仅对信号线有效，非信号线返回None
            - 返回值可能是SignalLabel实例或布尔值（False表示无标签）

        ### SetNote:
            - 支持多种参数类型：SignalLabel实例、字符串、布尔值
            - 仅对信号线有效，非信号线设置无效
            - 设置后会同步到父级数据源，确保标签配置一致
            - 字符串参数：设置为标签文字，使用默认样式
            - 布尔值参数：True启用默认标签，False禁用标签
        """
        if self._issignal:
            return self._plotinfo.signalstyle[self.sname].label

    @signal_label.setter
    def signal_label(self, value) -> None:
        """
        ## 设置信号标签配置
        """
        if self._issignal:
            source_signalstyle = self.__source._plotinfo.signalstyle[self.sname]
            line_signalstyle = self._plotinfo.signalstyle[self.sname]
            if isinstance(value, SignalLabel):
                value.set_position(self.sname, source_signalstyle.key)
                source_signalstyle.label = value
                line_signalstyle.label = value
            elif isinstance(value, str) and value:
                if not isinstance(source_signalstyle.label, SignalLabel):
                    source_signalstyle.set_default_label(self.sname)
                if not isinstance(line_signalstyle.label, SignalLabel):
                    line_signalstyle.set_default_label(self.sname)
                source_signalstyle.label.text = value
                line_signalstyle.label.text = value
            elif isinstance(value, bool):
                if value:
                    if not isinstance(source_signalstyle.label, SignalLabel):
                        source_signalstyle.set_default_label(self.sname)
                    if not isinstance(line_signalstyle.label, SignalLabel):
                        line_signalstyle.set_default_label(self.sname)
                else:
                    source_signalstyle.label = False
                    line_signalstyle.label = False

    def set_label(self,
                  text: str = "",
                  size: int = 10,
                  style: LabelStyle = "bold",
                  color: str = "red") -> None:
        """
        ## 快捷设置信号标签（仅信号线有效）

        ### 功能：
            - 简化信号标签配置流程，直接传入标签参数，自动构建/更新 SignalLabel 实例，并同步父级与自身配置；
            - 仅当 text 为非空字符串时生效（避免空标签）。

        Args:
            text (str): 标签文字内容（必填，非空才会执行设置）；
            size (int): 标签字体大小（单位：pt，内部自动拼接为 "XXpt" 格式）；
            style (Literal["normal", "bold"]): 标签字体样式（"normal" 正常，"bold" 加粗）；
            color (Colors): 标签字体颜色（支持 Colors 枚举成员或十六进制字符串）。

        Returns:
            None

        ### 示例
        ```python
        # 设置买入信号标签
        line_obj.set_label(
            text="买入",
            size=12,
            style="bold",
            color=Colors.red,
        )

        # 设置卖出信号标签
        line_obj.set_label(
            text="卖出",
            size=10,
            style="normal",
            color=Colors.green,
        )
        ```

        ### Note:
            - 仅对信号线有效，非信号线调用无效
            - text参数必须为非空字符串，否则不执行设置
            - 设置后会同步到父级数据源，确保标签显示一致
            - 系统默认设置：
                long_label = dict(size=10, style="bold", color="red")
                short_label = dict(size=10, style="bold", color="green")
                default_signal_label = {
                    "long_signal": SignalLabel("Long Entry", **long_label),    # 多头入场标签
                    "short_signal": SignalLabel("Short Entry", **short_label),  # 空头入场标签
                    "exitlong_signal": SignalLabel("Exit Long", **short_label),  # 多头离场标签
                    "exitshort_signal": SignalLabel("Exit Short", **long_label)  # 空头离场标签
                }
        """
        if self._issignal and isinstance(text, str) and text:
            self.__source._plotinfo.signalstyle[self.sname].set_label(
                text, size, style, color, name=self.sname)
            self._plotinfo.signalstyle[self.sname].set_label(
                text, size, style, color, name=self.sname)

    @property
    def isplot(self) -> bool:
        """
        ## 绘图开关（与父级非蜡烛图源数据同步）

        - Get：
            - 获取当前列的绘图状态（True 绘图、False 不绘图），
            - 取值自父级 source.isplot 中对应 sname 的状态；
        - Set：
            - 设置当前列的绘图状态，参数会自动转为 bool 类型；
            - 仅当父级 source.category != "candles"（非蜡烛图数据）时，才同步更新父级 source.isplot 中对应 sname 的状态，
            - 同时更新自身 _plotinfo.isplot，确保绘图状态一致。

        Args:
            value (Any): 绘图开关状态，任何可转换为布尔值的类型

        Returns:
            bool: 当前列的绘图状态，True表示绘制，False表示不绘制

        ### 示例
        ```python
        # 获取当前列的绘图状态
        plot_status = line_obj.isplot
        print(f"当前列是否绘制: {'是' if plot_status else '否'}")

        # 启用当前列的绘制
        line_obj.isplot = True

        # 禁用当前列的绘制
        line_obj.isplot = False

        # 使用数值控制
        line_obj.isplot = 1  # 启用绘制
        line_obj.isplot = 0  # 禁用绘制
        ```

        ### GetNote:
            - 返回值为布尔类型，True表示该列在图表中绘制，False表示不绘制
            - 取值来源是父级数据源中对应列名的绘图设置

        ### SetNote:
            - 参数会自动转换为布尔类型
            - 仅对非蜡烛图数据有效（父级category != 'candles'）
            - 设置后会同步到父级数据源的对应列绘图设置，确保显示一致性
            - 对于蜡烛图数据，设置仅影响当前Line对象的_plotinfo，不与父级同步
        """
        return self.__source.isplot[self.sname]

    @isplot.setter
    def isplot(self, value):
        """
        ## 设置绘图开关
        """
        value = bool(value)
        self._plotinfo.isplot = value
        if self.__source.category != 'candles':
            self.__source.isplot[self.sname] = value

    @property
    def source(self) -> IndFrame | KLine:
        """
        ## 父级源数据实例（只读）

        - 获取当前 Line 实例绑定的父级源数据（IndFrame 或 KLine 实例），用于访问父级其他列、合约信息等元数据；

        Returns:
            IndFrame | KLine: 当前Line实例绑定的父级源数据

        ### 示例
        ```python
        # 获取父级源数据
        parent_source = line_obj.source
        print(f"父级数据类型: {type(parent_source).__name__}")
        print(f"父级数据列: {parent_source.lines.tolist()}")

        # 通过父级源数据访问其他信息
        if isinstance(parent_source, KLine):
            print(f"合约代码: {parent_source.symbol}")
            print(f"数据频率: {parent_source.timeframe}")
        ```

        ### Note:
            - 此属性为只读，无setter，初始化时绑定后不可修改
            - 通过该属性可以访问父级的所有数据和方法
            - 对于IndFrame类型的父级，可以访问其他指标线
            - 对于KLine类型的父级，可以访问OHLC数据、合约信息等
        """
        return self.__source

    @property
    def overlap(self) -> bool:
        """
        ## 与主图重叠显示开关（与父级非蜡烛图源数据同步）

        - Get：
            - 获取当前列是否与主图重叠显示（True 重叠、False 单独显示），
            - 取值自父级 source.overlap 中对应 sname 的状态；
        - Set：
            - 设置当前列的重叠显示状态，参数会自动转为 bool 类型；
            - 仅当父级 source.iscandles 为 False（非蜡烛图数据）时，才同步更新父级 source.overlap 中对应 sname 的状态，
            - 同时更新自身 _plotinfo.overlap，确保重叠显示配置一致。

        Args:
            value (Any): 重叠显示开关状态，任何可转换为布尔值的类型

        Returns:
            bool: 当前列的重叠显示状态，True表示与主图重叠显示，False表示单独显示

        ### 示例
        ```python
        # 获取当前列的重叠显示状态
        overlap_status = line_obj.overlap
        print(f"当前列是否与主图重叠: {'是' if overlap_status else '否'}")

        # 设置当前列与主图重叠显示
        line_obj.overlap = True

        # 设置当前列单独显示（不重叠）
        line_obj.overlap = False

        # 对于非蜡烛图数据的设置效果
        if not line_obj.source.iscandles:
            line_obj.overlap = True  # 该设置会同步到父级
        ```

        ### GetNote:
            - 返回值为布尔类型，True表示与主图重叠显示，False表示单独显示
            - 取值来源是父级数据源中对应列名的重叠显示设置
            - 重叠显示适用于需要在同一坐标轴上叠加多个指标的情况

        ### SetNote:
            - 参数会自动转换为布尔类型
            - 仅对非蜡烛图数据有效（父级iscandles == False）
            - 设置后会同步到父级数据源的对应列重叠显示设置，确保显示一致性
            - 对于蜡烛图数据，设置仅影响当前Line对象的_plotinfo，不与父级同步
            - 重叠显示可以节省图表空间，但可能造成视觉混乱
        """
        return self.__source.overlap[self.sname]

    @overlap.setter
    def overlap(self, value):
        """
        ## 设置重叠显示开关
        """
        value = bool(value)
        if not self.__source.iscandles:
            self._plotinfo.overlap = value
            self.__source.overlap[self.sname] = value

    @property
    def follow(self) -> bool:
        """
        ## 主图跟随状态（只读，继承父级）

        - 获取当前列是否跟随主图显示（True 跟随、False 不跟随），优先继承父级 source.follow 属性（若父级有该属性），
        - 父级无 follow 属性时默认返回 True；

        Returns:
            bool: 当前列的主图跟随状态，True表示跟随主图，False表示不跟随

        ### 示例
        ```python
        # 获取当前列的主图跟随状态
        follow_status = line_obj.follow
        print(f"当前列是否跟随主图: {'是' if follow_status else '否'}")

        # 检查父级是否有follow属性
        if hasattr(line_obj.source, "follow"):
            print(f"父级follow属性值: {line_obj.source.follow}")
        else:
            print("父级没有follow属性，使用默认值True")
        ```

        ### Note:
            - 此属性为只读，无setter，状态完全由父级决定
            - 如果父级有follow属性，则继承该属性的值
            - 如果父级没有follow属性，则默认返回True
            - 跟随主图表示该列的显示范围、缩放等操作与主图同步
            - 常用于需要与主图价格坐标对齐的指标线
        """
        return self.__source.follow if hasattr(self.__source, "follow") else True


class CustomBase:
    """
    >>> lines: list[str] = []
        overlap: bool = True
        isplot: bool = True
        ismain: bool = False
        category: str | None = None
        plotinfo:PlotInfo=PlotInfo()
    """
    data: KLine | IndFrame | Line | IndSeries
    height: int = 150
    lines: list[str] | Lines[str] = Lines()
    overlap: bool = False
    isplot: bool = True
    ismain: bool = False
    category: str | None = None
    isindicator: bool = True
    # plotinfo: PlotInfo = PlotInfo()
    candlestyle: CandleStyle | None = None
    linestyle: Addict[str, LineStyle] | dict[str,
                                             LineStyle] = Addict()
    signalstyle: Addict[str, SignalStyle] | dict[str,
                                                 SignalStyle] = Addict()
    spanstyle: SpanList[SpanStyle] | list[SpanStyle] = SpanList()
    _bt_indicator_cls: dict[str,type[BtIndicator]] = {}

    @classmethod
    def _is_method_overridden(cls, method_name):
        """检查类是否重新定义了指定的实例方法"""
        import types
        return method_name in cls.__dict__ and isinstance(cls.__dict__[method_name], types.FunctionType)

    def next(self: KLine) -> IndFrame | IndSeries:
        return self.close if hasattr(self, "close") else self

    def step(self): ...

    @classmethod
    def _parse_return_variables(cls):
        # 获取next方法的所有源代码行
        func_lines, _ = getsourcelines(cls.next)

        # 预处理：去掉注释和空行
        cleaned_lines = []
        for line in func_lines:
            # 去掉行内注释
            if '#' in line:
                line = line.split('#')[0]
            stripped = line.strip()
            if stripped:
                cleaned_lines.append(stripped)

        # 合并续行（反斜杠 \ + 括号包裹的隐式续行）
        merged_lines = []
        current_line = ''
        for line in cleaned_lines:
            if current_line:
                current_line += ' ' + line
            else:
                current_line = line
            # 反斜杠显式续行
            if current_line.rstrip().endswith('\\'):
                current_line = current_line.rstrip()[:-1].strip()
            else:
                merged_lines.append(current_line)
                current_line = ''

        if current_line:
            merged_lines.append(current_line)

        # 合并括号包裹的多行表达式（如 return (\n a, \n b, \n)）
        bracket_merged = []
        i = 0
        while i < len(merged_lines):
            line = merged_lines[i]
            # 如果当前行以 return 开头且有未闭合的括号
            if line.strip().startswith('return'):
                open_count = line.count('(') - line.count(')')
                j = i + 1
                while open_count > 0 and j < len(merged_lines):
                    next_line = merged_lines[j]
                    line += ' ' + next_line
                    open_count += next_line.count('(') - next_line.count(')')
                    j += 1
                bracket_merged.append(line)
                i = j
            else:
                bracket_merged.append(line)
                i += 1
        merged_lines = bracket_merged

        # 查找最后一条return语句
        return_line = None
        for line in merged_lines:
            if line.startswith('return'):
                return_line = line

        if return_line:
            # 提取return后面的内容
            return_content = return_line.split('return', 1)[1].strip()
            if not return_content:
                return Lines()

            # 再次确保去除注释
            if '#' in return_content:
                return_content = return_content.split('#')[0].strip()

            # 分割变量名（按逗号）
            if "," in return_content:
                lines = tuple([var.strip()
                              for var in return_content.split(",")])
            else:
                lines = (return_content,)

            # 处理属性访问（如obj.var取var）和去除可能的括号
            processed_lines = []
            for ls in lines:
                # 去除可能的括号
                ls = ls.replace("(", "").replace(")", "").strip()
                # 再次确保去除注释
                if '#' in ls:
                    ls = ls.split('#')[0].strip()
                # 取最后一个点号后面的内容
                if "." in ls:
                    processed_lines.append(ls.split(".")[-1])
                else:
                    processed_lines.append(ls)

            return Lines(*processed_lines)
        return Lines()


class BtIndicator(CustomBase, KLine):
    """
    ## 自定义指标创建基类（继承 CustomBase 与 KLine）

    ### 核心定位
    - 提供标准化接口快速构建自定义技术指标，自动集成框架数据结构与绘图系统，
    - 支持将用户定义的计算逻辑转换为框架兼容的指标数据（IndSeries/IndFrame）

    ### 📘 **文档参考**:
    - 类简介：https://www.minibt.cn/minibt_basic/1.14minibt_btindicator_class_guide/

    ### 核心特性：
    1. 简化指标开发流程：
       - 通过重写 `next` 方法定义指标计算逻辑，无需关注数据格式转换与框架集成细节
       - 自动解析指标输出列名（`lines`），支持手动指定或从 `next` 方法返回值自动提取
    2. 完整的指标配置能力：
       - 继承 `CustomBase` 的绘图配置属性（`overlap` 重叠显示、`isplot` 绘图开关、`category` 指标分类等）
       - 支持自定义线型（`linestyle`）、信号样式（`signalstyle`）、绘图高度（`height`）等可视化参数
    3. 框架无缝集成：
       - 生成的指标自动兼容 `KLine`/`IndFrame` 数据结构，可直接用于策略逻辑或进一步计算
       - 通过 `@tobtind` 装饰器自动处理指标元数据（如指标ID、绘图信息），无需手动维护
    4. 参数化与灵活性：
       - 支持通过 `params` 属性定义指标参数，实现同一指标的多参数版本
       - 支持批量注册自定义方法到 `KLine` 类，扩展基础数据结构的指标计算能力

    ### 初始化参数说明：
    Args:
        data (IndFrame | pd.DataFrame): 输入数据，需为框架内置 `IndFrame` 或 pandas 的 `pd.DataFrame`
                                        （含计算所需基础字段如 open/close 等）
        **kwargs (IndSetting | dict): 指标配置参数，支持以下核心配置：
            - lines (list[str] | Lines): 指标输出列名列表（如 ["ma5", "ma10"]，
                                         未指定时自动从 `next` 方法返回值提取）
            - overlap (bool): 是否与主图重叠显示（默认继承 `CustomBase` 的 `overlap=False`）
            - isplot (bool): 是否绘图（默认 True）
            - ismain (bool): 是否为主图指标（默认 False）
            - category (str): 指标分类（如 "overlap" 归为重叠指标，默认 None）
            - linestyle (Addict[str, LineStyle] | dict[str, LineStyle]): 指标线样式配置
                - 覆盖规则：若键与 `lines` 中的列名匹配，优先使用该配置；未匹配项使用框架默认
            - signalstyle (Addict[str, SignalStyle] | dict[str, SignalStyle]): 交易信号线样式配置
                - 仅对 `lines` 中标记为信号的列（如 "long_signal"）生效
            - spanstyle (SpanList[SpanStyle] | list[SpanStyle]): 区间填充样式配置
                - 要求：`SpanStyle` 实例的 `start_line`/`end_line` 必须在 `lines` 中定义
            - _multi_index: 多索引标识（内部使用，用户一般无需设置）
            - 其他参数：会被合并到 `params` 属性，可在 `next` 方法中通过 `self.params` 访问

    ### ⚠️ 重要注意事项：
    1. **方法命名冲突风险**：
        - BtIndicator 类会在内部将自定义指标类的方法注入到传入的内置指标对象中
        - 如果自定义方法名与内置指标方法（如 `ma`, `atr`, `sma`, `ema` 等）相同，会导致覆盖
        - 覆盖后可能影响系统内其他使用相同内置指标的代码逻辑
        - **建议**：在自定义指标中使用独特的前缀或命名约定，避免与内置方法同名

    2. **注入机制说明**：
        - 自定义指标类中的方法（除 `next` 外）会被动态添加到输入数据对象的类中
        - 这种设计允许在 `next` 方法中方便地调用自定义方法
        - 但如果方法名冲突，会覆盖原有方法，可能导致不可预期的行为

    ### 关键方法说明：
    >>> next(self: KLine) -> IndFrame | IndSeries | pd.Series | pd.DataFrame | np.ndarray | tuple:
        - 核心方法，需用户重写，定义指标计算逻辑
        - 参数 `self`：通常为 `KLine` 实例，可直接访问其字段（如 `self.close` 获取收盘价序列）
        - 返回值：支持多种数据类型，框架会自动转换为 `IndSeries`（单列）或 `IndFrame`（多列）
        - 示例：返回 `self.close.rolling(5).mean()` 实现5期均线

    ### 使用示例：
    >>> # 1. 定义简单移动平均线指标
    >>> class MyMA(BtIndicator):
    ...     # 手动指定输出列名（也可省略，框架自动提取）
    ...     lines = ["ma5", "ma10"]
    ...     # 指标参数（可在初始化时通过 kwargs 覆盖）
    ...     params = {"length5": 5, "length10": 10}
    ...
    ...     # 注意：自定义方法命名避免与内置方法冲突
    ...     def _custom_calc_ma(self, data, length):
    ...         return data.rolling(length).mean()
    ...
    ...     def next(self):
    ...         # self 为 KLine 实例，可访问收盘价
    ...         ma5 = self._custom_calc_ma(self.close, self.params.length5)
    ...         ma10 = self._custom_calc_ma(self.close, self.params.length10)
    ...         return ma5, ma10  # 返回多列数据，对应 lines 定义
    ...
    >>> # 2. 使用自定义指标
    >>> # 假设 kline 为 KLine 实例（含 close 字段）
    >>> ma_indicator = MyMA(kline, isplot=True, category="overlap")
    >>> # 3. 访问指标结果（已自动转换为 IndFrame）
    >>> print(ma_indicator.ma5)  # 输出5期均线序列（Line类型）
    >>> print(ma_indicator.ma10) # 输出10期均线序列（Line类型）
    >>> # 4. 指标自动支持绘图，配置已通过类属性和初始化参数设置

    ### 命名建议：
    - 自定义方法使用下划线前缀（如 `_calc_...`）
    - 使用指标名称作为前缀（如 `myindicator_...`）
    - 避免使用内置指标常见的方法名（ma, atr, sma, ema, rsi, macd, etc.）
    - 保持方法名清晰描述其功能
    """
    params: Params = {}

    def __new__(cls, data: KLine | IndFrame | pd.DataFrame, **kwargs: IndSetting | dict) -> IndFrame | IndSeries:
        # 检查用户是否重写了 __init__ 方法
        has_init = cls._is_method_overridden("__init__")
        
        if "lines" in kwargs:
            # 用户通过 kwargs 显式设置了 lines
            cls.lines = Lines(*kwargs["lines"])
        else:
            if cls.lines:
                # 用户在类中已经定义了 lines 属性
                pass
            elif has_init:
                # 用户重写了 __init__，但没有设置 lines
                # 保持 lines 为空，让用户在 __init__ 中通过 self.lines.xxx = ... 来设置
                cls.lines = Lines()
            else:
                # 用户没有重写 __init__，尝试从 next 方法解析返回变量
                cls.lines = cls._parse_return_variables()

        # 确保lines中不包含注释
        if cls.lines:
            # 过滤掉空字符串和只包含注释的项
            clean_lines = []
            for line in cls.lines:
                # 去除注释
                if '#' in line:
                    line = line.split('#')[0].strip()
                # 只保留非空项
                if line:
                    clean_lines.append(line)

            cls.lines = Lines(*clean_lines)

        if cls.lines:
            if isinstance(cls.lines, (list, tuple)):
                cls.lines = Lines(*cls.lines)
            assert cls.lines and isinstance(cls.lines, Lines) and all(
                [isinstance(l, str) for l in cls.lines]), "lines类型为tuple or list,元素为字符串"
        # print(data.lines._ind_obj,data.lines.l_close)
        #cls.lines(data)
        # line = data.line
        # if line:
        #     for name,line in zip(data.lines,data.line):
        #         object.__setattr__(cls.lines, name, line)
        # else:
        #     object.__setattr__(cls.lines, data.lines[0], data)
        height = kwargs.pop("height", cls.height)
        overlap = kwargs.pop('overlap', cls.overlap)
        isplot = kwargs.pop('isplot', cls.isplot)
        ismain = kwargs.pop('ismain', cls.ismain)
        category = kwargs.pop('category', cls.category)
        multi_index = kwargs.pop("_multi_index", None)
        candlestyle = kwargs.pop("candlestyle", cls.candlestyle)
        linestyle = kwargs.pop("linestyle", cls.linestyle)
        signalstyle = Addict(kwargs.pop("signalstyle", cls.signalstyle))
        spanstyle = kwargs.pop("spanstyle", cls.spanstyle)
        isindicator = kwargs.pop("isindicator", cls.isindicator)
        params = kwargs.pop("params", {})
        for k,v in kwargs.items():
            if k in cls.params:
                params[k]=v
        cls.params = Addict({**cls.params, **params})
        kline_dict = data.__dict__
        # 只注入用户在子类中自定义的属性和方法，排除 CustomBase 已有属性
        # 排除 __init__ 等魔术方法（会覆盖 IndFrame 的构造器），
        # name-mangling 后的 private 方法（如 _KalmanFilter__test）不受影响
        [setattr(data.__class__, k, v) for k, v in cls.__dict__.items()
            if k not in kline_dict
            and k != "next"
            and not k.startswith("__")
            and k not in CustomBase.__dict__]

        # 如果用户重写了 __init__，需要先将输入数据的列绑定到 lines 上
        has_init = cls._is_method_overridden("__init__")
        if has_init:
            data.__dict__['params'] = cls.params
            cls.__init__(data)

        @tobtind(lines=cls.lines, overlap=overlap, isplot=isplot, ismain=ismain,
                 category=category, myself=cls.__name__, _multi_index=multi_index,
                 linestyle=linestyle, signalstyle=signalstyle, spanstyle=spanstyle, isindicator=isindicator)
        def _next(self):
            self.__dict__['params'] = cls.params
            if has_init:
                return None
            return cls.next(self)
        indicator = _next(data)
        if cls._is_method_overridden("step"):
            setattr(indicator, "step", lambda: cls.step(indicator))
        # 保存原始指标类，供 signal_backtest 优化时重新创建不同参数的指标实例
        indicator._bt_indicator_cls = cls
        cls._bt_indicator_cls[cls.__name__]=cls
        return indicator
    
    
class StopMeta(type):
    """
    Stop类的元类（核心逻辑：__init__(kline,**kwargs) → 绑定kline → 调用user_init）
    """
    def __new__(cls, name, bases, attrs):
        # 跳过Stop基类本身的处理
        if name == "Stop":
            return super().__new__(cls, name, bases, attrs)

        # 标记是否已处理，避免重复改造
        if attrs.get("_is_stop_processed"):
            return super().__new__(cls, name, bases, attrs)

        # 判断是否为Stop的直接子类
        is_direct_subclass = any(base.__name__ == "Stop" for base in bases)

        if is_direct_subclass:
            # 保存用户定义的__init__
            user_init = attrs.get("__init__")

            # 定义最终的__init__：先关联KLine及定义一些常量，再运行用户初始化
            def new_init(self: Stop, kline: KLine, **kwargs):
                """
                最终的初始化方法（对用户透明）
                :param kline: K线对象（核心绑定）
                :param kwargs: 用户传入的所有业务参数
                """
                self._kline = kline
                self._price_tick = kline.price_tick
                self._volume_multiple = kline.volume_multiple

                if user_init:
                    if options._data_patching:
                        self.kline._reset_kline()
                        user_init(self, **kwargs)
                        self._kline._update_replace()
                    else:
                        user_init(self, **kwargs)

            # 替换子类的__init__为新的核心初始化方法
            attrs["__init__"] = new_init
            attrs["_is_stop_processed"] = True

        # 创建并返回类对象
        return super().__new__(cls, name, bases, attrs)


class Stop(metaclass=StopMeta):
    """## 停止类

    ### 常用属性
        >>> self.kline (KLine) : 合约数据
        self.price_tick (float) : 最小变化单位
        self.volume_multiple (float) :合约剩数
        self.stop_price (Line[float]): 停止价
        self.target_price (Line[float]): 目标价序列
        self.kline.open_price (float): 交易价格序列
        self.close (Line): 收盘价，或
        self.kline.close
        self.close[-1] (float): 最新价格
        self.stop_price[-2] (float): 前一停止价
        self.target_price[-2] (float): 前一目标价
        self.kline.stop_lines[-1] (np.ndarray[float]): 最新停止价和目标价

    ### 定义__init__,long和short方法
        >>> __init__初始化常量及基础计算
        long和short方法计算最新停止价或目标价
        通过以下方法进行赋值
        self.stop_price.new = new_stop_price
        self.target_price.new = new_target_price

    ### Examples
    ```python
    class SegmentationTracking(Stop):
        \"\"\"## 分段跟踪停止策略
        根据价格波动幅度分段调整止损位置，结合ATR、标准差等指标

        ## Args:
            length (int, optional): atr指标长度. Defaults to 14.
            mult (float, optional): atr指标乘数. Defaults to 1..
            method (str, optional): 初始价选用指标,可选["atr","std","smoothrng"]. Defaults to "atr".
            acceleration (list[float], optional): 分段乘数. Defaults to [0.382, 0.5, 0.618, 1.0].
            min_distance (float, optional): 最小距离. Defaults to 0..
        \"\"\"

        def __init__(self, length: int = 14, mult: float = 1., method: Literal["atr", "std", "smoothrng"] = "atr",
                    acceleration: list[float] = [0.382, 0.5, 0.618, 1.0], min_distance: float = 0.) -> None:
            self.length = length  # 指标计算周期长度
            self.mult = mult  # 指标乘数
            self.method = method  # 选用的指标方法
            self.acceleration = acceleration  # 分段加速度（调整系数）
            # 计算最小距离（基于加速度的第一个值）
            self.min_distance = max(min(1., min_distance), 0.) * acceleration[0]
            # 根据方法选择对应的指标函数
            if self.method == 'atr':
                self._atr = self.kline.atr(
                    self.length)
            elif self.method == 'std':
                self._atr = self.kline.close.stdev(
                    self.length)
            else:
                self._atr = self.kline.close.btind.smoothrng(
                    self.length)

        def long(self):
            \"\"\"多单分段跟踪止损计算\"\"\"
            _atr = self._atr[-1]
            if not np.isnan(_atr):
                pre_stop_price = self.stop_price[-2]
                # 初始止损设置
                if np.isnan(pre_stop_price):
                    stop_price = self.low[-1] - self.mult * _atr
                else:
                    preprice = self.close[-2]  # 前收盘价
                    lastprice = self.close[-1]  # 最新收盘价
                    stop_price = pre_stop_price  # 上次止损价

                    # 若最新收盘价高于前收盘价（价格上涨）
                    if lastprice > preprice:
                        diff_price = lastprice - self.kline.open_price  # 与开仓价的差价
                        _atr *= self.mult  # 应用乘数
                        range_ = lastprice - preprice  # 价格波动范围

                        # 根据不同条件调整止损价
                        if stop_price < self.kline.open_price:
                            # 止损价低于开仓价时，用最小加速度调整
                            stop_price += self.acceleration[0] * range_
                        else:
                            # 根据差价与ATR的关系，使用不同加速度
                            if diff_price < _atr:
                                stop_price += self.acceleration[1] * range_
                            elif diff_price < 2. * _atr:
                                stop_price += self.acceleration[2] * range_
                            else:
                                stop_price += self.acceleration[3] * range_
                    else:
                        # 价格未上涨时，应用最小距离调整
                        if self.min_distance:
                            stop_price += self.min_distance * range_
                self.stop_price.new = stop_price  # 更新止损价

        def short(self):
            \"\"\"空单分段跟踪止损计算\"\"\"
            _atr = self._atr[-1]
            if not np.isnan(_atr):
                pre_stop_price = self.stop_price[-2]
                # 若为初始止损设置
                if np.isnan(pre_stop_price):
                    stop_price = self.high[-1] + self.mult * _atr
                else:
                    preprice = self.close[-2]  # 前收盘价
                    lastprice = self.close[-1]  # 最新收盘价
                    stop_price = pre_stop_price  # 上次止损价

                    # 若前收盘价高于最新收盘价（价格下跌）
                    if preprice > lastprice:
                        diff_price = self.kline.open_price - lastprice  # 与开仓价的差价
                        _atr *= self.mult  # 应用乘数
                        range_ = preprice - lastprice  # 价格波动范围

                        # 根据不同条件调整止损价
                        if stop_price > self.kline.open_price:
                            # 止损价高于开仓价时，用最小加速度调整
                            stop_price -= self.acceleration[0] * range_
                        else:
                            # 根据差价与ATR的关系，使用不同加速度
                            if diff_price < _atr:
                                stop_price -= self.acceleration[1] * range_
                            elif diff_price < 2. * _atr:
                                stop_price -= self.acceleration[2] * range_
                            else:
                                stop_price -= self.acceleration[3] * range_
                    else:
                        # 价格未下跌时，应用最小距离调整
                        if self.min_distance:
                            stop_price += self.min_distance * range_
                self.stop_price.new = stop_price  # 更新止损价
    ```
    """
    _kline: KLine
    _price_tick: float
    _volume_multiple: float
    _is_stop_processed: bool

    def __init__(self, ** kwargs):
        raise NotImplementedError("子类必须实现__init__方法")

    @property
    def kline(self) -> KLine:
        """## K线（KLine）"""
        return self._kline

    @property
    def price_tick(self) -> float:
        """## 合约最小变动单位"""
        return self._price_tick

    @property
    def volume_multiple(self) -> float:
        """## 合约剩数"""
        return self._volume_multiple

    @property
    def datetime(self) -> Line[datetime.datetime]:
        """## 时间序列"""
        return self.kline.datetime

    @property
    def open(self) -> Line[float]:
        """## 开盘价序列"""
        return self.kline.open

    @property
    def high(self) -> Line[float]:
        """## 最高价序列"""
        return self.kline.high

    @property
    def low(self) -> Line[float]:
        """## 最低价序列"""
        return self.kline.low

    @property
    def close(self) -> Line[float]:
        """## 收盘价序列"""
        return self.kline.close

    @property
    def volume(self) -> Line[float]:
        """## 成交量序列"""
        return self.kline.volume

    @property
    def index(self) -> int:
        return self.kline.btindex

    @property
    def stop_price(self) -> Line[float]:
        return self.kline.stop_lines.stop_price

    @property
    def target_price(self) -> Line[float]:
        return self.kline.stop_lines.target_price

    def long(self) -> None:
        """## 多头停止计算"""
        ...

    def short(self) -> None:
        """## 空头停止计算"""
        ...

    def update(self) -> bool:
        """## 更新
        """
        pos = self.kline.position.pos
        if pos:
            if pos > 0:
                self.long()
                if self.close[-1] <= self.stop_price[-1] or self.close[-1] >= self.target_price[-1]:
                    self.kline.set_target_size()
            else:
                self.short()
                if self.close[-1] >= self.stop_price[-1] or self.close[-1] <= self.target_price[-1]:
                    self.kline.set_target_size()
        else:
            pass


class IndicatorClass(metaclass=Meta):
    """## 第三方指标库集成"""
    PandasTa = PandasTa
    BtInd = BtInd
    TqFunc = TqFunc
    TqTa = TqTa
    TaLib = TaLib
    FinTa = FinTa
    TuLip = TuLip
    Pair = Pair
    Factors = Factors


class BtIndType(metaclass=Meta):
    """## 内置指标类型"""
    Line = Line
    IndFrame = IndFrame
    IndSeries = IndSeries


class KLineType(metaclass=Meta):
    """## KLine类型"""
    KLine = KLine
    
def _call_(self:pd.DataFrame | pd.Series, *args, **kwargs)->KLine | IndFrame | IndSeries:
    if self.ndim == 1:
        return IndSeries(self,**kwargs)
    else:
        lines=list(self.columns)
        if set(lines).issuperset(FILED.ALL):
            return KLine(self[FILED.ALL],**kwargs)
        else:
            return IndFrame(self,lines=lines,**kwargs)

pd.DataFrame.__call__ = _call_
pd.Series.__call__ = _call_
