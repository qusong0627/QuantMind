from __future__ import annotations
import re
from ..utils import \
    (TYPE_CHECKING, PlotInfo, np, pd, wraps, Callable, BtID, retry_with_different_params,
     partial, Iterable, Lines, Base, check_type, LineStyle, IndSetting, SymbolInfo, 
     DataFrameSet, Addict, Any, cachedmethod, attrgetter, LineDash, Colors,
     Multiply, get_lennan, BlockPlacement, ispandasojb, rolling_method,
     Category, SIGNAL_Str, set_property, FILED,
     default_symbol_info,  warnings,ExpandingGroupby,ExponentialMovingWindowGroupby,RollingGroupby,
     StrategyInstances, TPE, TqObjs, default_signal_style,
     DataSetBase, PandasObject, Rolling, CandleStyle, 
     pandas_method, dataclass, DefaultIndicatorConfig,  CategoryString, SpanList, 
     signature,  ExponentialMovingWindow, 
     ewm_method, Expanding, expanding_method, options, SPECIAL_FUNC,)
from ..other import get_func_args_dict, timedelta
from pandas.core.indexing import _iLocIndexer, _LocIndexer

# Pandas 2.0+ Copy-on-Write 兼容性处理
_PANDAS_VERSION = tuple(map(int, pd.__version__.split('.')[:2]))
if _PANDAS_VERSION >= (2, 0):
    pd.set_option('mode.copy_on_write', False)


if TYPE_CHECKING:
    from typing_ import *
    from .tradingview import TradingView
    from ..strategy.strategy import Strategy
    from ..bt import Bt
    from ..utils import CoreFunc, corefunc, Params
    from .core import *
    from typing import Concatenate
    from typing import ParamSpec
    from .pandas_ta import PandasTa
    from .talib import TaLib
    from .tulip import TuLip
    from .btind import BtInd
    from .finta import FinTa
    from .tqfunc import TqFunc
    from .tqta import TqTa
    from .pair import Pair
    from .factors import Factors
    from ..tradingsymtem.tradingsystem import TradingSystem


class SeriesType:
    """IndFrame指标线Line转IndSeries"""

    def __init__(self, indframe: IndFrame):
        self.indframe = indframe

    def __getattr__(self, key) -> IndSeries:
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
            False,
        )
        return IndSeries(getattr(self.indframe, key).values, **config.to_dict())


# minibt框架中的索引器基类和自定义索引器类
# 用于处理金融时间序列数据的索引操作，支持pandas兼容的索引方式
class MinibtIndexerBase:
    """
    minibt索引器基类 - 为所有索引器提供通用功能
    """

    def __init__(self, name: str, obj: KLine | IndFrame | IndSeries | Line):
        self.name = name
        self.obj = obj

    def _convert_to_minibt(self, data: pd.DataFrame | pd.Series) -> KLine | IndFrame | IndSeries:
        """
        智能转换为minibt数据结构
        """
        if options.check_conversion_mode(data, self.obj):
            return self._create_minibt_object(data)

        return data

    def _create_minibt_object(self, pandas_obj: pd.DataFrame | pd.Series) -> KLine | IndFrame | IndSeries:
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
                    indicator_kwargs["lines"] = list(
                        pandas_obj.columns)
                    return self.obj._get_indframe()(pandas_obj.values, **indicator_kwargs)

            # 默认返回原对象
            return pandas_obj

        except Exception as e:
            # 其他错误，记录并返回原始对象
            warnings.warn(f"转换内置指标失败: {e}")
            return pandas_obj

    def _is_kline_data(self, pandas_obj: pd.DataFrame) -> bool:
        """
        检查是否为K线数据
        """
        try:
            # return all(field in FILED.ALL for field in pandas_obj.columns)
            return set(FILED.ALL) == set(pandas_obj.columns)
        except:
            return False

    def _create_kline(self, pandas_obj: pd.DataFrame, **kwargs) -> KLine:
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
            ...
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

    def __getitem__(self, key) -> KLine:
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

    def __init__(self, name: str, obj: KLine | IndFrame | IndSeries | Line):
        # 分别调用两个父类的__init__
        MinibtIndexerBase.__init__(self, name, obj)
        _LocIndexer.__init__(self, name, obj)

    def __getitem__(self, key) -> KLine:
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


def _sanitize_column_name(name: str) -> str:
    """清理列名，确保合法性：转小写、替换非法字符、处理纯数字开头"""
    #name = str(name).lower()           # 转小写
    name = re.sub(r'\.+', '_', name)    # "." 替换为 "_"
    name = re.sub(r'[^a-z0-9_]', '', name)  # 移除非字母数字下划线字符
    name = re.sub(r'^(\d)', r'_\1', name)    # 数字开头的列名加前缀 "_"
    name = name.strip('_')              # 去除首尾下划线
    return name if name else 'unnamed'  # 空名称时使用默认值


def tobtind(lines: list[str] | dict[str, bool] | None = None, overlap: dict | bool = False,
            isplot: bool = True, category: str | None = None, **kwargs_):
    """
    ### minibt 框架指标装饰器 —— 将任意技术指标计算函数适配为 minibt 内置指标类型
    ---
    本装饰器是整个指标系统的**入口枢纽**：它接收一个指标计算函数（可以是自定义函数或 PandasTa
    等第三方库的方法），在执行该函数的前后自动完成数据源提取、参数注入、格式校验、维度自适应、
    KLine 回测重放（replay）以及最终将返回数据封装为 ``IndSeries`` / ``IndFrame`` 的全流程。

    #### 核心职责（按执行顺序）
    1. **定位数据源** —— 从 ``args[0]``（KLine / IndFrame / IndSeries / Line）中提取底层
       ``pandas_object`` / ``_df``，作为指标计算的原材料。
    2. **决议计算函数** —— 若指定了 ``lib``（默认 ``"pta"``，即 PandasTa），优先从
       ``source.ta`` 查找 ``{lib}_{func_name}`` 或 ``{func_name}``；否则直接使用被装饰的
       原始函数。
    3. **参数融合与覆盖** —— 装饰器级的默认值可被调用时传入的同名 ``kwargs`` 动态覆盖
       （如 ``lines`` / ``overlap`` / ``category`` / ``isplot`` 等），实现"一装饰多场景"。
    4. **回测重放（replay）** —— 当数据源标记 ``isreplay=True`` 且为完整 K 线（OHLCVA）
       时，启用分段历史重放计算，避免偷看未来数据。
    5. **类型校验与维度规整** —— 返回结果必须为 ``pd.DataFrame`` / ``pd.Series`` /
       ``np.ndarray``；自动将单列矩阵降维为向量，合并多返回值，推导 ``lines`` 列名。
    6. **封装为框架类型** —— 将最终 numpy 数组包装为 ``IndSeries``（单线）或
       ``IndFrame``（多线），并附带 ID、名称、绘图属性、线型样式等元信息。

    #### 典型用法
    ```python
    # 1) 使用 PandasTa 内置指标（隐式绑定 lib='pta'）
    @tobtind(lines=['fast', 'slow'], overlap=True)
    def ema8_21(source):
        ...

    # 2) 自定义指标函数（myself=True, lib=None）
    @tobtind(myself=True, lib=None, lines=['signal'])
    def my_oscillator(source, period):
        ...

    # 3) 策略中调用（覆盖装饰器默认值）
    self.sma = self.bt.pta_ema(self.kline, length=20, overlap=True, category='overlap')
    ```

    Args:
        lines:
            - **IndFrame** 指标须显式设置列名，**IndSeries** 指标默认 ``None``。
            - 可传入 ``list[str]`` 或 ``dict[str, bool]``；字典格式会在包装阶段自动展平为列表。
            - 若运行时仍未提供，会自动从 ``pd.DataFrame.columns`` 清洗生成。
        overlap:
            - ``True`` / ``dict``：主图叠加（在价格图表上绘制）；``False``（默认）：副图显示。
            - 若传入 ``dict``，其键为线名、值为布尔标志，可精确控制每条线的叠加行为。
        isplot:
            是否在图表中显示该指标。默认 ``True``；设为 ``False`` 时只计算不绘图。
        category:
            - ``'overlap'`` / ``True``：主图叠加；``'candles'``：蜡烛图；
              ``None`` / ``False``：无类别（默认副图）。
            - 归类决定指标在 GUI / Web 图表中的渲染区域。
        **kwargs_:
            - ``lib`` (str)：指标库名，默认 ``"pta"``（PandasTa）。设为 ``None`` 则使用被装饰原始函数。
            - ``isindicator`` (bool)：内部标志，表示当前装饰链是"指标"调用（默认 ``False``）。
            - ``_multi_index`` (int|None)：多周期指标索引，用于多时间框架下配对返回值。
            - ``linestyle`` (dict)：线型/颜色/宽度配置，如 ``{'fast':'k-','slow':'r--'}``。
            - ``sname`` (str)：策略内简写名，用于引用时区分同一指标的不同参数实例。
            - ``signalstyle`` (dict)：信号图层的样式配置。
            - ``myself`` (str|bool)：传入自定义名时表示走"自定义指标"分支；该分支直接调用被装饰函数自身。
            - ``iscustom`` (bool)：内部自定义标志，透传给 ``IndFrame`` / ``IndSeries``。
            - ``ismain`` (bool)：是否主周期指标（多时间框架场景），默认 ``False``。
            - ``light_chart`` (bool)：轻量图表模式，会为数据追加 ``"time"`` 列。

    Returns:
        Callable：三层嵌套的装饰器，最终返回 ``wrapper`` 函数。
        wrapper 被调用时返回 ``IndFrame | IndSeries``（若设置了 ``_multi_index``，
        则返回 ``(index, data)`` 元组）。

    Raises:
        AssertionError:
            - 指标返回数据不是 ``pd.DataFrame | pd.Series | np.ndarray``。
            - 自定义指标模式下 ``lines`` 数量与实际数据维度不匹配。
            - 自定义指标函数既未返回数据，也未通过 ``source.lines`` 注入。
    """

    # ---- 闭包捕获装饰器级默认参数，供 wrapper 运行时回退使用 ----
    _isplot = isplot                    # 是否绘图标志
    _lines = lines                      # 指标线配置
    _overlap = overlap                  # 主图叠加配置
    _category = category                # 指标类别
    _lib = kwargs_.pop('lib', "pta")                   # 指标库名称，默认 "pta"（PandasTa）
    _isindicator = kwargs_.pop('isindicator', False)   # 是否为指标函数标志
    _index = kwargs_.pop('_multi_index', None)         # 多周期指标索引
    _linestyle = kwargs_.pop('linestyle', {})          # 线型样式 {线名: 样式码}
    _sname = kwargs_.pop("sname", "")                  # 策略中使用的简写名
    _signalstyle = kwargs_.pop('signalstyle', {})      # 信号图层样式配置

    def decorator(func):
        """
        ### 中间层装饰器 —— 捕获被装饰的原始指标计算函数 ``func``

        这一层的作用是将 ``func`` 保存到闭包中，并返回最终的执行体 ``wrapper``。
        当用户调用 ``@tobtind(lines=['a','b'], overlap=True)`` 时，
        ``tobtind(...)`` 立即执行并返回 ``decorator``，随后 ``decorator(func)`` 返回 ``wrapper``，
        此后对 ``func`` 的所有调用都被 ``wrapper`` 接管。

        Args:
            func: 被装饰的原始指标计算函数。

        Returns:
            wrapper: 包装后的指标函数，返回 ``IndSeries`` 或 ``IndFrame`` 对象。
        """

        @wraps(func)
        def wrapper(*args, **kwargs) -> IndFrame | IndSeries:
            """### 包装执行函数 —— 指标计算的完整生命周期

            这是 `@tobtind` 的核心运行体，负责串联"数据准备 → 函数决议 →
            参数融合 → 计算执行 → 格式转换 → 框架封装"六大步骤。"""

            # ==================================================================
            # 阶段一：数据源识别与计算函数决议
            # ==================================================================

            # 1. 记录原始函数名，提取数据源（args[0] 约定为源数据）
            func_name = func.__name__
            _source: KLine | IndFrame | IndSeries | Line = args[0]

            # 1a. 解包包装类（BtInd/Talib/FinTa/TuLip/PandasTa/TqFunc/TqTa 等）
            # 这些类持有 _df 但自身无 _dataset，直接作为 source 会导致 btplot 追溯 KLine 失败
            _source_for_ind = _source
            while hasattr(_source_for_ind, '_df') and not hasattr(_source_for_ind, '_dataset'):
                _source_for_ind = _source_for_ind._df

            # 2. 从数据源继承指标属性（_indsetting），如已有 ID、绘图开关等
            source_indsetting = _source.get_indicator_kwargs(isindicator=True) if hasattr(
                _source, "_indsetting") else {}

            # 3. 判断是否自定义指标 —— 自定义指标默认使用本地函数自身，不查 ta 库
            myself = kwargs_.pop('myself', False)
            if myself:
                # 3a. 自定义指标分支：强制使用被装饰函数 func
                func_name = myself                  # 允许用自定义名覆盖内置函数名
                ind_func = func                     # 实际执行的函数 = 被装饰函数
                _use_source_arg = True              # func(source, *params) 需要显式传入 source
                source = _source                    # 数据源保持原样
            else:
                # 3b. 内置 / 库指标分支：先提取纯数据 DataFrame，再从 ta 扩展中查找函数
                if hasattr(_source, "pandas_object"):
                    # 当 KLine 的 follow=False 且为蜡烛图类型时，使用原始K线数据
                    # （kline_object），而非变换后的数据（如 HA / 线性回归K线）
                    if (hasattr(_source, "follow") and hasattr(_source, "kline_object")
                            and not _source.follow and _source.kline_object is not None):
                        source = _source.kline_object  # 使用原始K线数据（变换前）
                    else:
                        source = _source.pandas_object  # KLine 等类型取其底层 DataFrame
                elif hasattr(_source, "_df"):
                    source = _source._df            # 其他指标类型取其 _df
                else:
                    source = _source.copy()         # 回退：直接拷贝
                # print(source.head())

                # 决议计算函数：优先 ta.{lib}_{func_name} → ta.{func_name} → func
                if _lib:
                    prefixed_name = f"{_lib}_{func_name}"
                    ta = getattr(source, "ta")      # 获取 PandasTa 入口对象
                    if hasattr(ta, prefixed_name):
                        # 带库名前缀的函数（如 pta_ema）已将 source 绑定为 self，无需再传
                        ind_func: Callable = getattr(ta, prefixed_name)
                        func_name = prefixed_name
                        _use_source_arg = False
                    elif hasattr(ta, func_name):
                        # 裸函数名（如 cdl_doji），调用时需传入 (source, *params, **kwargs)
                        ind_func: Callable = getattr(ta, func_name)
                        _use_source_arg = True
                    else:
                        # ta 库中未找到 → 回退到被装饰的 func
                        ind_func = func
                        _use_source_arg = True
                else:
                    # lib=None → 强制使用被装饰函数
                    ind_func = func
                    _use_source_arg = True
                # print(ind_func,func_name,_use_source_arg)

            # ==================================================================
            # 阶段二：参数融合 —— 运行时 kwargs 可覆盖装饰器默认值
            # ==================================================================
            sname = kwargs.pop("sname", _sname or func_name)      # 策略内简写名
            ind_name = func_name                            # 内部指标名 = 函数名
            category = kwargs.pop('category', _category)          # 指标分类
            isplot = kwargs.pop('isplot', _isplot)                # 是否绘图
            ismain = kwargs.pop('ismain', False)                  # 是否主周期
            lines = kwargs.pop('lines', _lines)                   # 指标线名称
            overlap = kwargs.pop("overlap", _overlap)             # 主图叠加
            light_chart = kwargs.pop("light_chart", False)        # 轻量图表模式
            index = kwargs.get("_multi_index", _index)            # 多周期索引
            isindicator = kwargs.get("isindicator", _isindicator) # 是否指标链
            iscustom = kwargs.get("iscustom", False)              # 自定义标志
            linestyle = kwargs.get("linestyle", _linestyle)       # 线型样式
            signalstyle = kwargs.get("signalstyle", _signalstyle) # 信号样式

            # 4. 指标实例 ID —— 优先使用调用方传入的，其次继承数据源的，最后自动生成
            id = kwargs.get("id", source_indsetting.get("id", BtID()))

            # 5. 回测相关标志（重采样 / 重放）
            isresample = source_indsetting.get(
                "isresample", kwargs.get("isresample", False))
            isreplay = source_indsetting.get(
                "isreplay", kwargs.get("isreplay", False))

            # 6. 提取指标参数（去掉第一个数据源位置参数后，剩余均为指标参数）
            ind_params = list(args[1:])

            # ==================================================================
            # 阶段三：执行指标计算（重放模式 vs 正常模式）
            # ==================================================================

            if isreplay and len(source.shape) > 1 and set(FILED) == set(source.columns):
                # ---------- 重放模式（回测专用）----------
                # 在回测场景中，不能用整段历史数据直接计算指标（会导致"未来数据泄露"）。
                # 重放模式将历史 K 线按分段长度逐步喂给指标函数，仅返回当期结果。

                num_cols = len(_lines) if _lines else 1               # 预期列数
                nan_data = [np.nan] * num_cols if _lines else np.nan  # 不足时用 NaN 填充
                replay_length = 100                                   # 默认窗口长度

                # 6a. 从 kwargs 和 ind_params 中提取最大整数参数作为窗口长度
                if kwargs:
                    for _, v in kwargs.items():
                        if isinstance(v, int):
                            replay_length = max(replay_length, v)

                v = [arg for arg in ind_params if isinstance(arg, int)]
                if v:
                    replay_length = max(v)  # 取最大整数值作为窗口长度
                else:
                    replay_length = 100

                # 6b. 构建多级长度列表（用于自适应重试）
                length_ls = [replay_length + 100 * i
                             for i in range(1, int(len(source) / 100))]

                # 6c. 核心计算函数 —— 对给定长度的数据切片计算最后一个指标值
                def core(index=0, data_=None, length=100):
                    """对长度为 length 的尾部数据计算指标，返回 (index, 最后一个值)"""
                    if len(data_) < length:
                        return index, nan_data          # 数据不够 → 返回 NaN
                    if myself:
                        _args = [data_]
                        _args.extend(ind_params)
                        return index, func(*_args, **kwargs).values[-1]
                    else:
                        return index, getattr(getattr(data_, "ta"), func_name)(
                            *ind_params, **kwargs).values[-1]

                # 6d. 获取回放专用 K 线数据源
                klines = source.source._replay_datas(source.ind_name) if type(
                    source) == Line else source._replay_datas()

                # 6e. 自适应重试：先用最短长度，失败则递增长度重试
                @retry_with_different_params(length_ls)
                def retry(length):
                    return TPE.run(partial(core, length=length), klines, **kwargs)

                # 6f. 执行重放计算
                ind_data = retry()

            else:
                # ---------- 正常模式 ----------
                if myself:
                    # —— 自定义指标分支 ——
                    _args = [source]
                    _args.extend(ind_params)
                    ind_data = func(*_args, **kwargs)          # 直接调用被装饰函数

                    # 特殊处理：某些自定义函数通过修改 source.lines 返回数据，而不显式返回
                    if ind_data is None and type(source.lines) == Lines and source.lines._lines:
                        lines, ind_data = list(source.lines._lines.keys()), list(
                            source.lines._lines.values())

                    # 统一为 list 以便后续统一处理
                    if not isinstance(ind_data, (tuple, list)):
                        ind_data = [ind_data]

                    # 校验所有返回数据均为合法类型
                    assert check_type(ind_data), \
                        "返回数据格式须为 (pd.DataFrame, pd.Series, np.ndarray)"

                    ind_data = list(ind_data)

                    # 统计所有返回数据包含的总线数
                    lines_num = sum(
                        [1 if len(d.shape) == 1 else d.shape[1] for d in ind_data])

                    # lines 数量与数据维度不一致时，尝试自动展开 DataFrame 列名补救
                    if lines and len(lines) != lines_num:
                        try:
                            if any([isinstance(idata, pd.DataFrame) for idata in ind_data]):
                                _new_lines = []
                                for l, idata in zip(lines, ind_data):
                                    if isinstance(idata, pd.DataFrame):
                                        _new_lines.extend(list(idata.columns))
                                    else:
                                        _new_lines.append(l)
                                lines = _new_lines
                        except:
                            ...  # 展开失败则跳过，后续由断言捕获

                    # 断言：指标数据非空，lines 数量匹配
                    assert ind_data is not None or source.lines._lines, \
                        "自定义指标未产出任何数据"
                    assert lines and len(lines) == lines_num, \
                        f"请为 '{func_name}' 设置正确的 lines（期望 {lines_num} 条，实际 {len(lines) if lines else 0} 条）"

                else:
                    # —— 库函数指标分支（如 PandasTa）——
                    if _use_source_arg:
                        ind_data = ind_func(source, *ind_params, **kwargs)   # 未绑定方法，需传 source
                    else:
                        ind_data = ind_func(*ind_params, **kwargs)           # 已绑定方法

                    # DataFrame 结果：若未显式提供 lines 或数量不匹配，从列名自动推导
                    if isinstance(ind_data, pd.DataFrame):
                        _actual_cols = ind_data.shape[1]
                        if lines is None or (isinstance(lines, Iterable) and len(lines) != _actual_cols):
                            _orig_lines = lines
                            lines = [_sanitize_column_name(col) for col in ind_data.columns]
                            if _orig_lines is not None:
                                warnings.warn(
                                    f"【{func_name}】传入的 lines({_orig_lines}, {len(_orig_lines)}列) "
                                    f"与实际数据列数({_actual_cols}列)不匹配，已自动修正为: {lines}",
                                    UserWarning, stacklevel=3)

            # ==================================================================
            # 阶段四：数据类型校验、规整与合拢
            # ==================================================================

            assert check_type(ind_data), \
                f"指标返回非合法数据类型: {type(ind_data)}，" \
                f"必须为 pd.DataFrame / pd.Series / np.ndarray"

            # 规范化 overlap 类型
            if not isinstance(overlap, (bool, dict)):
                overlap = False

            # dict 格式的 lines 展平为 list
            if lines and isinstance(lines, dict):
                lines = [lines.get(l, l) for l in lines]

            # 将 list/tuple 返回值合并为统一 numpy 数组
            if isinstance(ind_data, (list, tuple)):
                if len(ind_data) == 1:
                    data = ind_data[0]
                else:
                    data = np.c_[tuple([data if isinstance(data, np.ndarray)
                                        else data.values for data in ind_data])]
            else:
                data = ind_data if isinstance(ind_data, np.ndarray) else ind_data.values

            # 单列矩阵降维为向量（形状 (N,1) → (N,)）
            if len(data.shape) > 1 and data.shape[1] == 1:
                data = data[:, 0]

            # ==================================================================
            # 阶段五：封装为 minibt 指标类型
            # ==================================================================

            if len(data.shape) > 1:
                # —— IndFrame（多线指标）——
                from .core import IndFrame

                if not lines:
                    lines = [f"line{i}" for i in range(data.shape[1])]

                data = IndFrame(data, id=id, sname=sname, ind_name=ind_name, lines=Lines(*lines),
                                category=category, isplot=isplot, ismain=ismain,
                                isreplay=isreplay, isresample=isresample,
                                overlap=overlap, isindicator=isindicator, iscustom=iscustom,
                                linestyle=linestyle, signalstyle=signalstyle, source=_source_for_ind)
            else:
                # —— IndSeries（单线指标）——
                from .core import IndSeries

                data = IndSeries(data, id=id, sname=sname, ind_name=ind_name, lines=Lines(ind_name),
                                 category=category, isplot=isplot, ismain=ismain,
                                 isreplay=isreplay, isresample=isresample,
                                 overlap=overlap, isindicator=isindicator, iscustom=iscustom,
                                 linestyle=linestyle, source=_source_for_ind)

            # 轻量图表模式：拼接时间列
            if light_chart:
                data = pd.concat([source.datetime, data], axis=1)
                data.rename(columns=dict(datetime="time"), inplace=True)
                data.category = category
                data.overlap = overlap

            # 多周期场景：返回 (index, data) 元组供调用方配对
            if index is not None:
                return index, data

            return data

        # 将闭包参数挂载到 wrapper 上，供外部反射读取（如获取默认 overlap）
        wrapper._decorator_args = {
            "overlap": _overlap,
        }

        return wrapper

    return decorator


class BtBaseWindows:
    """
    ## 量化回测框架中的窗口操作基类
    - 为滚动窗口、指数加权移动窗口、扩展窗口提供统一的基础功能封装

    ### 核心功能：
    - 1. 统一封装 Pandas 窗口操作类的通用逻辑（Rolling、ExponentialMovingWindow、Expanding）
    - 2. 通过装饰器模式重写窗口方法，自动将返回结果转换为框架内置的 IndSeries/IndFrame 类型
    - 3. 提供统一的参数分离机制，区分原生窗口参数和框架扩展参数
    - 4. 自动继承和增强指标配置（名称、绘图设置、数据重叠等）

    ### 设计模式：
    - 装饰器模式：通过 _wrap_pandas_rolling_method_to_indicator 装饰原生窗口方法
    - 模板方法模式：为子类提供统一的窗口操作处理流程
    - 属性拦截模式：通过 __getattribute__ 动态拦截和包装方法调用

    ### 继承关系：
        BtBaseWindows
          ├── BtRolling
          ├── BtExponentialMovingWindow
          └── BtExpanding

    ### 使用场景：
    - 技术指标计算（移动平均、标准差、相关系数等）
    - 时序数据分析（滚动统计、指数平滑、累积计算）
    - 量化策略开发（动量指标、波动率指标、趋势指标）
    """

    _obj: IndFrame | IndSeries | Line
    """输入数据对象，必须是框架支持的 IndSeries/IndFrame/Line 类型"""

    _base_object: Rolling | ExponentialMovingWindow | Expanding
    """底层的 Pandas 窗口对象，执行实际的窗口计算"""

    _method: set[str]
    """该窗口类型支持的方法名称集合，用于属性访问拦截"""

    def __init__(self, obj: IndFrame | IndSeries | Line, method_list: set[str]):
        """
        ## 初始化窗口操作基类

        Args:
            obj: 输入数据对象，必须包含 pandas_object 属性（存储原生 Pandas 对象）
            method_list: 该窗口类型支持的方法名称集合，用于动态方法包装

        ### 设计说明：
        - 将 method_list 作为实例属性传入，避免类属性在继承时的循环引用问题
        - 子类负责创建具体的 _base_object（Rolling/ExponentialMovingWindow/Expanding）
        - 统一的初始化流程确保所有窗口类具有一致的行为
        """
        self._obj = obj
        self._method = method_list  # 作为实例属性传入，避免循环引用
        # 断言校验：输入对象必须包含 'pandas_object' 属性（确保是框架支持的数据类型）
        # pandas_object 用于存储原生 Pandas Series/DataFrame，供后续滚动计算使用
        assert hasattr(
            self._obj, 'pandas_object'), f'{self.__class__.__name__}数据对象必须是IndSeries、IndFrame或Line类型（需包含pandas_object属性）'

    def _wrap_pandas_rolling_method_to_indicator(self, func: Callable) -> Callable:
        """
        ## 类装饰器：将 Pandas 窗口原生方法的返回结果，转换为框架内置的指标数据类型

        ### 核心作用：
        - 1. 保留原生方法的计算逻辑，仅增强返回结果的类型适配
        - 2. 自动补充指标配置参数（如名称、计算设置），便于策略管理指标
        - 3. 根据返回数据的维度（Series/DataFrame），生成对应的框架数据类型
        - 4. 支持参数分离，区分窗口计算参数和框架扩展参数

        ### 实现机制：
        - 使用 functools.wraps 保留原函数的元信息（名称、文档字符串等）
        - 通过 inspect.signature 动态分析参数签名，实现智能参数分离
        - 根据返回数据的形状和类型，自动选择转换为 IndSeries 或 IndFrame

        Args:
            func: 待装饰的函数（Pandas 窗口原生方法，如 mean、std、sum 等）

        Returns:
            装饰后的函数：返回框架内置的 IndSeries/IndFrame 类型，或原结果（非 Pandas 对象时）

        ### 处理流程：
        - 1. 参数分离 → 2. 调用原生方法 → 3. 类型检查 → 4. 配置增强 → 5. 类型转换

        示例：
        >>> # 装饰前：返回 pd.Series
        >>> result = self._base_object.mean()
        >>> type(result)  # pandas.core.IndSeries.Series
        >>>
        >>> # 装饰后：返回 framework.IndSeries
        >>> result = wrapped_mean()
        >>> type(result)  # minibt.core.IndSeries

        ### 注意事项：
        - 确保输入数据对象具有 pandas_object 属性
        - 转换前检查数据长度一致性（data.shape[0] == self._obj.V）
        - 自动处理指标名称和配置参数的继承与增强
        """
        @wraps(func)  # 保留原方法的元信息（名称、文档字符串、注解等）
        def wrapper(*args, **kwargs):
            """
            ## 装饰器内部包装函数：执行原生计算并转换返回类型

            ### 参数处理逻辑：
            - 分离为 method_kwargs（传递给Pandas原生方法）
            - 分离为 indicator_kwargs（框架指标扩展参数）
            - 支持 *args 位置参数传递

            ### 类型转换条件：
            - 结果是 pd.Series 或 pd.DataFrame 类型
            - 数据长度与输入对象一致（data.shape[0] == self._obj.V）
            - 非 Pandas 对象（如标量、None）直接返回

            ### 配置增强逻辑：
            - 自动设置 isindicator=True 标记
            - 继承原指标的 sname、ind_name 等配置
            - 生成合理的指标名称（格式：原指标名_窗口方法名）
            - 自动处理 lines 配置（单列→[func_name]，多列→data.columns）
            """
            # 1. 获取原生方法的参数签名，确定参数分类边界
            sig = signature(func)
            params = sig.parameters.keys()

            # 2. 智能参数分离：窗口计算参数 vs 框架扩展参数
            method_kwargs = {}  # 传递给 Pandas 原生窗口方法的参数
            indicator_kwargs = {}  # 框架指标的扩展参数（名称、绘图、重叠等）
            func_name = func.__name__

            for key, value in kwargs.items():
                if key in params:
                    method_kwargs[key] = value  # 窗口方法原生参数
                else:
                    indicator_kwargs[key] = value  # 框架扩展参数

            # 3. 调用底层 Pandas 窗口方法执行实际计算
            data: pd.Series | pd.DataFrame = getattr(
                self._base_object, func_name)(*args, **method_kwargs)

            # 4. 类型检查和转换：仅对 Pandas 对象且长度一致的数据进行转换
            if options.check_conversion_mode(data, self._obj):
                # 4.1 配置增强：标记为指标并继承原对象配置
                indicator_kwargs.update(dict(isindicator=True))
                indicator_kwargs = self._obj.get_indicator_kwargs(
                    **indicator_kwargs)

                # 4.2 名称处理：生成合理的指标名称
                sname = indicator_kwargs.get("sname", func_name)
                ind_name = indicator_kwargs.get("ind_name", func_name)
                indicator_kwargs.pop('lines', None)  # 移除原有 lines 配置

                # 4.3 名称增强：格式为"原指标名_窗口方法名"（如 close_rolling_mean）
                indicator_kwargs['sname'] = f"{sname}_{func_name}"
                indicator_kwargs["ind_name"] = f"{ind_name}_{func_name}"

                # 4.4 多列数据（DataFrame）处理
                if len(data.shape) > 1:  # 二维数据，多列输出
                    indicator_kwargs["lines"] = list(data.columns)
                    return self._obj._get_indframe()(data.values, **indicator_kwargs)

                # 4.5 单列数据（Series）处理
                else:
                    indicator_kwargs['lines'] = [func_name,]
                    return self._obj._get_indseries()(data.values, **indicator_kwargs)

            # 5. 非 Pandas 对象或长度不匹配时直接返回（如标量、None 等）
            return data

        return wrapper

    def __getattribute__(self, item) -> KLine | IndFrame | IndSeries | Line:
        """
        ## 重写属性访问方法：动态拦截窗口方法调用并应用装饰器

        ### 核心机制：
        - 通过属性名称拦截，对支持的窗口方法自动应用类型转换装饰器
        - 使用 object.__getattribute__ 安全访问 _method 属性，避免循环引用
        - 保持非窗口方法属性的正常访问逻辑

        ### 拦截逻辑：
        - 检查属性名是否在 _method 集合中（预设的窗口方法列表）
        - 是：获取原始方法 → 应用装饰器 → 返回包装后的方法
        - 否：正常属性访问逻辑

        Args:
            item: 要访问的属性名（如 'mean'、'std'、'sum' 或其他属性）

        Returns:
            - 窗口方法：返回装饰后的方法（自动转换返回类型）
            - 其他属性：直接返回父类处理结果

        ### 技术细节：
        - 使用 object.__getattribute__ 绕过常规属性查找，避免递归
        - 装饰器应用时机：在方法被调用前动态包装，不影响方法定义
        - 保持方法调用的透明性，用户无需感知底层转换逻辑

        示例：
        >>> rolling_obj = self.data.close.rolling(10)
        >>> # 以下调用会自动被拦截和装饰：
        # 返回 framework.IndSeries
        >>> mean_indicator = rolling_obj.mean(overlap=True)
        >>> std_indicator = rolling_obj.std()  # 返回 framework.IndSeries
        >>> # 非窗口方法正常访问：
        >>> window_size = rolling_obj.window  # 直接返回整数值
        """
        # 安全获取 _method 集合，避免触发 __getattribute__ 递归
        _method_set = object.__getattribute__(self, '_method')

        # 拦截窗口方法调用：应用类型转换装饰器
        if item in _method_set:
            original_method = super().__getattribute__(item)
            return self._wrap_pandas_rolling_method_to_indicator(original_method)

        # 非窗口方法：正常属性访问逻辑
        return super().__getattribute__(item)


class BtRolling(BtBaseWindows):
    """
    ## 量化回测框架中的滚动窗口（Rolling）增强类，继承自基础 Rolling 类
    ### 核心功能：
    - 1. 封装 Pandas 的 Rolling 功能，限制输入数据类型为框架支持的 IndSeries/IndFrame/Line
    - 2. 重写属性访问逻辑，将 Pandas Rolling 原生方法（如 mean、std）的返回结果
       自动转换为框架内置的 IndSeries/IndFrame 类型（而非原生 Pandas 对象）
    - 3. 自动补充指标配置参数（如名称、计算设置），适配回测策略的指标管理体系

    Args:
        obj: 输入数据对象（必须是框架自定义的 IndFrame/IndSeries/Line 类型，需包含 pandas_object 属性）
        window: 滚动窗口大小（如 5 表示 5 期窗口，支持 int/offset 等 Pandas 兼容类型）
        min_periods: 窗口内最小非空值数量（低于此数量则结果为 NaN，默认 None 即等于 window 大小）
        center: 是否将窗口结果对齐到窗口中心（默认 False，对齐到窗口末尾）
        win_type: 窗口权重类型（默认 None 为等权重，支持 'boxcar'/'triang' 等 Pandas 支持类型）
        on: 用于滚动计算的列名（仅 IndFrame 有效，默认 None 表示用所有列）
        axis: 滚动轴方向（0 为按行滚动/时间维度，1 为按列滚动/特征维度，默认 0）
        closed: 窗口闭合方式（如 'right' 表示包含右边界，默认 None 跟随 Pandas 规则）
        step: 窗口步长（默认 None 为 1，即每步滑动 1 个单位）
        method: 计算方法（默认 'single'，预留参数用于扩展批量计算逻辑）
    """

    def __init__(self,
                 obj: IndFrame | IndSeries | Line,
                 window=None,
                 min_periods=None,
                 center=False,
                 win_type=None,
                 on=None,
                 axis=0,
                 closed=None,
                 step=None,
                 method='single',
                 ):
        """
        ## 初始化 BtRolling 实例（滚动窗口配置）

        Args:
            obj: 输入数据对象（必须是框架自定义的 IndFrame/IndSeries/Line 类型，需包含 pandas_object 属性）
            window: 滚动窗口大小（如 5 表示 5 期窗口，支持 int/offset 等 Pandas 兼容类型）
            min_periods: 窗口内最小非空值数量（低于此数量则结果为 NaN，默认 None 即等于 window 大小）
            center: 是否将窗口结果对齐到窗口中心（默认 False，对齐到窗口末尾）
            win_type: 窗口权重类型（默认 None 为等权重，支持 'boxcar'/'triang' 等 Pandas 支持类型）
            on: 用于滚动计算的列名（仅 IndFrame 有效，默认 None 表示用所有列）
            axis: 滚动轴方向（0 为按行滚动/时间维度，1 为按列滚动/特征维度，默认 0）
            closed: 窗口闭合方式（如 'right' 表示包含右边界，默认 None 跟随 Pandas 规则）
            step: 窗口步长（默认 None 为 1，即每步滑动 1 个单位）
            method: 计算方法（默认 'single'，预留参数用于扩展批量计算逻辑）

        ### 关键逻辑：
        - 校验输入数据对象必须包含 'pandas_object' 属性（存储原生 Pandas 对象，如 pd.Series/pd.DataFrame）
        - 调用父类 Rolling 的初始化方法，传入原生 Pandas 对象和滚动参数
        """
        super().__init__(obj, rolling_method)

        # 调用父类 Rolling 的构造方法，初始化滚动窗口（传入原生 Pandas 对象和配置参数）
        # pandas 3.0+ 不再支持 axis 参数
        rolling_kwargs = dict(
            window=window,
            min_periods=min_periods,
            center=center,
            win_type=win_type,
            on=on,
            closed=closed,
            step=step,
            method=method
        )
        if _PANDAS_VERSION < (3, 0):
            rolling_kwargs['axis'] = axis
        self._base_object = Rolling(
            self._obj.pandas_object,  # 父类需要原生 Pandas 对象进行滚动计算
            **rolling_kwargs
        )

    def skew(self, numeric_only: bool = False, **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算滚动窗口内的偏度（衡量数据分布不对称性）
        Args:
            numeric_only: 是否仅对数值型列计算（默认 False）
            **kwargs: 框架扩展参数（如指标名称、绘图配置等）
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def cov(self, other=None, pairwise=None, ddof=1, **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算滚动窗口内的协方差（衡量两变量线性相关程度）
        Args:
            other: 对比变量（默认 None，计算自身协方差）
            pairwise: 是否两两计算协方差（默认 None，跟随 pandas 规则）
            ddof: 自由度（默认 1，样本协方差）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def mean(self, numeric_only: bool = False, **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算滚动窗口内的均值（平均值）
        Args:
            numeric_only: 是否仅对数值型列计算（默认 False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def agg(self, func, engine:Literal["cython", "numba"] | None=None, engine_kwargs:dict[str, bool] | None=None, **kwargs) -> IndFrame | IndSeries:
        """
        ## 滚动窗口内的聚合计算（支持自定义函数）
        Args:
            func: 聚合函数（如 'sum'、lambda 或函数列表）
            engine: 计算引擎（默认 None，用 pandas 原生引擎）
            engine_kwargs: 引擎参数（默认 None）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def count(self, numeric_only: bool = False, **kwargs) -> IndFrame | IndSeries:
        """
        ## 统计滚动窗口内的非空值数量
        Args:
            numeric_only: 是否仅统计数值型列（默认 False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def quantile(self, q=0.5, interpolation='linear', numeric_only: bool = False, **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算滚动窗口内的分位数（默认中位数，q=0.5）
        Args:
            q: 分位数（如 0.25 为四分位数，0.5 为中位数）
            interpolation: 插值方式（默认 'linear'，处理分位数落在两数之间的情况）
            numeric_only: 是否仅对数值型列计算（默认 False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def max(self, numeric_only: bool = False, **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算滚动窗口内的最大值
        Args:
            numeric_only: 是否仅对数值型列计算（默认 False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def aggregate(self, func, engine:Literal["cython", "numba"] | None=None, engine_kwargs:dict[str, bool] | None=None, **kwargs) -> IndFrame | IndSeries:
        """
        ## 同 agg 方法，滚动窗口内的聚合计算（支持自定义函数）
        Args:
            参数同 agg 方法
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def corr(self, other=None, pairwise=None, ddof=1, **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算滚动窗口内的相关系数（衡量两变量线性相关强度，范围 [-1,1]）
        Args:
            other: 对比变量（默认 None，计算自身相关）
            pairwise: 是否两两计算相关系数（默认 None，跟随 pandas 规则）
            ddof: 自由度（默认 1，样本相关系数）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def rank(self, axis=0, method='average', na_option='keep', ascending=True, pct=False, **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算滚动窗口内的排名（对窗口内数据排序并分配名次）
        Args:
            axis: 排名轴（默认 0，按行排名）
            method: 排名方式（默认 'average'，相同值取平均名次）
            na_option: NaN 处理（默认 'keep'，保留 NaN 不参与排名）
            ascending: 是否升序（默认 True，小值排名靠前）
            pct: 是否返回百分比排名（默认 False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def sem(self, numeric_only: bool = False, ddof=1, **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算滚动窗口内的标准误（均值的标准偏差，反映均值的抽样误差）
        Args:
            numeric_only: 是否仅对数值型列计算（默认 False）
            ddof: 自由度（默认 1）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def var(self, numeric_only: bool = False, ddof=1, **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算滚动窗口内的方差（衡量数据离散程度）
        Args:
            numeric_only: 是否仅对数值型列计算（默认 False）
            ddof: 自由度（默认 1，样本方差）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def apply(self, func, raw=False, engine:Literal["cython", "numba"] | None=None, engine_kwargs:dict[str, bool] | None=None, args=None, **kwargs) -> IndFrame | IndSeries:
        """
        ## 对滚动窗口内的数据应用自定义函数（灵活扩展计算逻辑）
        Args:
            func: 自定义函数（输入为窗口数据，返回计算结果）
            raw: 是否传入原始数组（默认 False，传入 Series/DataFrame）
            engine: 计算引擎（默认 None，用 pandas 原生引擎）
            args: 函数额外参数（默认 None）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def sum(self, numeric_only: bool = False, min_count=0, **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算滚动窗口内的求和
        Args:
            numeric_only: 是否仅对数值型列计算（默认 False）
            min_count: 最小非空值数量（默认 0，不足则结果为 0）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def min(self, numeric_only: bool = False, **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算滚动窗口内的最小值
        Args:
            numeric_only: 是否仅对数值型列计算（默认 False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def kurt(self, numeric_only: bool = False, fisher=True, bias=False, **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算滚动窗口内的峰度（衡量数据分布陡峭程度）
        Args:
            numeric_only: 是否仅对数值型列计算（默认 False）
            fisher: 是否返回 Fisher 峰度（默认 True，减去 3 使正态分布峰度为 0）
            bias: 是否修正偏差（默认 False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def median(self, numeric_only: bool = False, **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算滚动窗口内的中位数（数据排序后的中间值，抗极端值）
        Args:
            numeric_only: 是否仅对数值型列计算（默认 False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...

    def std(self, numeric_only: bool = False, ddof=1, **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算滚动窗口内的标准差（方差的平方根，衡量数据离散程度）
        Args:
            numeric_only: 是否仅对数值型列计算（默认 False）
            ddof: 自由度（默认 1，样本标准差）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的 IndFrame/IndSeries 类型结果
        """
        ...


class BtRollingGroupby(BtRolling):
    """
    ## 量化回测框架中的分组滚动窗口（Rolling Groupby）增强类

    - 继承自 ``BtRolling``，在滚动窗口的基础上增加分组（Groupby）能力
    - 每个分组内独立进行滚动窗口计算，分组间互不干扰

    ### 核心功能：
    - 1. 支持按指定列（或列组合）对数据进行分组后，在组内做滚动窗口统计计算
    - 2. 内部构造 pandas ``RollingGroupby`` 对象，利用 ``BaseGrouper`` 管理分组索引
    - 3. 通过父类 ``BtBaseWindows`` 的装饰器机制，将返回结果自动转换为框架内置的 IndSeries / IndFrame 类型
    - 4. 保留 ``BtRolling`` 的全部方法（mean、std、sum、aggregate 等），每个方法按组独立计算

    ### 设计模式：
    - 适配器模式：封装 pandas ``RollingGroupby`` 构造流程
    - 装饰器模式：继承父类 ``__getattribute__`` 拦截逻辑，自动包装窗口方法

    ### 继承关系：
        BtBaseWindows → BtRolling → BtRollingGroupby

    ### 使用场景：
    - 不同行业 / 板块的分组滚动均线计算（按 sector 分组）
    - 多品种分组的历史波动率统计
    - 分组内排名指标的滚动计算

    Examples:
        >>> # 按 sector 分组，计算每组的 20 日滚动均值
        >>> rg = BtRollingGroupby(df, by="sector", window=20)
        >>> rg.mean()  # 每组独立的 rolling mean
        >>> rg.std()   # 每组独立的 rolling std

        >>> # 按多列分组 + 指定时间窗口
        >>> rg = BtRollingGroupby(df, by=["sector", "exchange"], window="5D")
        >>> rg.quantile(0.75)
    """

    # _base_object 实际为 RollingGroupby 类型（继承链 BaseWindowGroupby + Rolling）
    # BtBaseWindows._base_object 注解为 Rolling | ExponentialMovingWindow | Expanding，
    # RollingGroupby 是 Rolling 的子类，因此类型兼容
    _base_object: RollingGroupby
    """底层 pandas RollingGroupby 对象，按组执行滚动窗口计算"""

    def __init__(
        self,
        obj: IndFrame | IndSeries | Line,
        by,
        window=None,
        min_periods=None,
        center=False,
        win_type=None,
        on=None,
        axis=0,
        closed=None,
        step=None,
        method="single",
        selection=None,
    ) -> None:
        """
        ## 初始化 BtRollingGroupby 实例（分组滚动窗口配置）

        内部通过 ``pandas.DataFrame.groupby(by)`` 获取分组器（``BaseGrouper``），
        再将其传入 ``RollingGroupby`` 构造器，使每个分组独立维护自己的滚动窗口。

        ### 与 BtRolling 的关键差异：
        - 新增 ``by`` 参数指定分组列
        - 新增 ``selection`` 参数指定参与计算的列
        - 内部不直接构造 ``Rolling(obj)``，而是构造 ``RollingGroupby(groupby._selected_obj, _grouper=...)``
        - ``groupby._selected_obj`` 已自动去除分组列，避免分组列参与窗口计算

        ### 实现流程：
        1. 调用父类 ``BtRolling.__init__`` 完成基础窗口配置（``_obj``、``_method`` 等）
        2. 对 ``_obj.pandas_object`` 执行 ``groupby(by)``，获取 ``_grouper`` 和 ``_selected_obj``
        3. 将 ``_selected_obj`` 作为数据，``_grouper`` 作为分组器，构造 ``RollingGroupby``
        4. pandas 底层自动使用 ``GroupbyIndexer`` 包裹 ``RollingIndexer``，实现组内独立窗口滑动

        Args:
            obj: 输入数据对象（必须是框架自定义的 IndFrame / IndSeries / Line 类型，需包含 pandas_object 属性）
            by: 分组依据，遵循 pandas ``DataFrame.groupby(by)`` 的参数规范：
                - ``str``：单个列名（如 ``"sector"``）
                - ``list[str]``：多个列名（如 ``["sector", "exchange"]``）
                - ``pd.Series``：自定义分组序列
                - 其他 pandas groupby 支持的 ``by`` 参数形式
            window: 滚动窗口大小（如 20 表示 20 期窗口，支持 int / offset / timedelta 等 Pandas 兼容类型）
            min_periods: 窗口内最小非空值数量（低于此数量则结果为 NaN，默认 None 即等于 window 大小）
            center: 是否将窗口结果对齐到窗口中心（默认 False，对齐到窗口末尾）
            win_type: 窗口权重类型（默认 None 为等权重，支持 'boxcar' / 'triang' 等 Pandas 支持类型）
            on: 用于滚动计算的列名（仅 IndFrame 有效，默认 None 表示用所有列）
            axis: 滚动轴方向（0 为按行滚动 / 时间维度，1 为按列滚动 / 特征维度，默认 0）
                pandas 3.0+ 不再支持 axis 参数，框架内部自动兼容
            closed: 窗口闭合方式（如 'right' 表示包含右边界，默认 None 跟随 Pandas 规则）
            step: 窗口步长（默认 None 为 1，即每步滑动 1 个单位）
            method: 计算方法（默认 ``"single"``，预留参数用于扩展批量计算逻辑）
            selection: 选择特定列参与计算（默认 None，表示所有数值列）

        ### 关键逻辑：
        - 校验输入数据对象必须包含 ``'pandas_object'`` 属性（由父类 ``BtBaseWindows.__init__`` 完成）
        - 自动从 ``pandas_object`` 中去除分组列，避免分组列参与数值计算
        - 每个分组独立维护滚动窗口，分组间边界不会互相跨越
        - 继承 ``BtRolling`` 的全部方法集，无需重复定义

        Raises:
            ValueError: 当 ``by`` 参数无法用于构造有效的分组器时（由 pandas ``groupby()`` 和 ``RollingGroupby`` 内部抛出）

        Examples:
            >>> # 按 sector 列分组，20 日滚动均值
            >>> rg = BtRollingGroupby(df, by="sector", window=20)
            >>> rg.mean()
            >>>
            >>> # 按多列分组 + 指定计算列
            >>> rg = BtRollingGroupby(df, by=["sector", "exchange"], window=10, selection="close")
            >>> rg.std()
        """
        # 1. 调用父类 BtRolling.__init__ → BtBaseWindows.__init__
        #    完成 _obj 赋值、_method 设置、pandas_object 属性校验
        super().__init__(
            obj,
            window=window,
            min_periods=min_periods,
            center=center,
            win_type=win_type,
            on=on,
            axis=axis,
            closed=closed,
            step=step,
            method=method,
        )
        # 父类 BtRolling.__init__ 内部已创建 self._base_object = Rolling(...)，
        # 下面将用 RollingGroupby 覆盖之，这是有意为之的设计

        # 2. 对原生 pandas 对象执行 groupby，获取分组器 _grouper 和去分组列后的数据 _selected_obj
        #    groupby 内部通过 BaseGrouper 管理分组索引，RollingGroupby 会使用它来按组计算
        groupby_obj = self._obj.pandas_object.groupby(by)

        # 3. 构建 RollingGroupby 所需的参数
        #    - _grouper: 必须参数，BaseGrouper 对象，管理分组索引
        #    - _as_index: 是否将分组列作为结果索引（默认 True，与 pandas 行为一致）
        rolling_kwargs = dict(
            window=window,
            min_periods=min_periods,
            center=center,
            win_type=win_type,
            on=on,
            closed=closed,
            step=step,
            method=method,
            selection=selection,
            _grouper=groupby_obj._grouper,
            _as_index=True,
        )
        if _PANDAS_VERSION < (3, 0):
            rolling_kwargs['axis'] = axis

        # 4. 使用 groupby._selected_obj（已丢弃分组列的数据）构造 RollingGroupby
        #    BaseWindowGroupby.__init__ 内部会再次 drop 分组列，此为 pandas 自身的安全机制
        self._base_object = RollingGroupby(
            groupby_obj._selected_obj,
            **rolling_kwargs
        )


class BtOnlineExponentialMovingWindow(BtBaseWindows):
    """
    ## 量化回测框架中的在线指数加权移动窗口（Online Exponential Moving Window）增强类

    ### 核心功能：
    1. 封装 Pandas 3.0+ 的 ``OnlineExponentialMovingWindow`` 功能，支持流式/增量计算
    2. 重写属性访问逻辑，将在线 EWM 原生方法的返回结果自动转换为框架内置的
       ``IndSeries`` / ``IndFrame`` 类型
    3. 自动补充指标配置参数（如名称、计算设置），适配回测策略的指标管理体系
    4. 支持增量更新（update）和状态重置（reset），适合实时行情流处理场景

    ### 使用场景：
    - 实盘/模拟交易中逐笔推送 K 线，需要增量更新指标（避免重复计算全部历史）
    - 在线机器学习中的特征滚动计算
    - 流式数据处理管道

    ### 与 BtExponentialMovingWindow 的关系：
    - ``BtExponentialMovingWindow.online()`` 返回本类实例
    - 本类封装了 Pandas ``ExponentialMovingWindow.online()`` 返回的原生对象

    Args:
        obj: 输入数据对象（必须是框架自定义的 IndFrame/IndSeries/Line 类型）
        com: 质心衰减参数（alpha = 1 / (1 + com)，与其他衰减参数互斥）
        span: 时间跨度衰减参数（alpha = 2 / (span + 1)，与其他衰减参数互斥）
        halflife: 半衰期衰减参数（权重减半所需时间，与其他衰减参数互斥）
        alpha: 直接平滑因子（值在 0-1 之间，与其他衰减参数互斥）
        min_periods: 窗口内最小非空值数量（默认 0）
        adjust: 是否使用精确权重计算（默认 True）
        ignore_na: 是否忽略 NaN 值（默认 False）
        times: 时间序列（仅当 halflife 为时间类型时有效，默认 None）
        engine: 计算引擎（默认 "numba"，用于加速在线聚合计算）
        engine_kwargs: 引擎参数字典（默认 None）
        selection: 选择特定列计算（默认 None，表示所有列）
    """

    def __init__(
        self,
        obj: IndFrame | IndSeries | Line,
        com: float | None = None,
        span: float | None = None,
        halflife: float | timedelta | np.timedelta64 | np.int64 | float | str | None = None,
        alpha: float | None = None,
        min_periods: int | None = 0,
        adjust: bool = True,
        ignore_na: bool = False,
        times: np.ndarray |  pd.Series | pd.DataFrame  | None = None,
        engine: Literal["cython", "numba"] | None = "numba",
        engine_kwargs: dict[str, bool] | None = None,
        *,
        selection=None,
    ) -> None:
        """
        ## 初始化 BtOnlineExponentialMovingWindow 实例（在线指数加权移动窗口配置）

        ### 关键逻辑：
        - 校验输入数据对象必须包含 'pandas_object' 属性
        - 检查衰减参数互斥性（com、span、halflife、alpha 只能使用其中一个）
        - 先构造底层 ``ExponentialMovingWindow``，再调用 ``.online()`` 获取在线计算对象
        - ``_base_object`` 为 Pandas ``OnlineExponentialMovingWindow`` 实例
        """
        # 在线 EWM 支持的方法集合（与 Pandas OnlineExponentialMovingWindow 对齐）
        online_ewm_method = {'reset', 'aggregate', 'std', 'corr', 'cov', 'var', 'mean'}
        super().__init__(obj, online_ewm_method)

        # ---- 参数互斥性检查（与 BtExponentialMovingWindow 保持一致） ----
        non_none_params = [p for p in [com, span, halflife, alpha] if p is not None]
        if len(non_none_params) > 1:
            raise ValueError("com、span、halflife 和 alpha 参数是互斥的，只能指定其中一个")

        # 如果没有指定任何衰减参数，使用默认的 com=0.5
        if len(non_none_params) == 0:
            com = 0.5

        # ---- 构造底层 ExponentialMovingWindow，再获取其在线版本 ----
        ewm_kwargs = dict(
            com=com,
            span=span,
            halflife=halflife,
            alpha=alpha,
            min_periods=min_periods,
            adjust=adjust,
            ignore_na=ignore_na,
            times=times,
            method="single",
        )
        # 先创建普通 EWM 对象，再调用 .online() 转为在线增量计算模式
        ewm = ExponentialMovingWindow(self._obj.pandas_object, **ewm_kwargs)
        self._base_object = ewm.online(engine=engine, engine_kwargs=engine_kwargs or {})

    def reset(self) -> None:
        """
        ## 重置在线 EWM 的内部状态

        将之前通过 ``update`` 累积的权重和中间结果清空，回到初始状态。
        重置后下一次调用 ``mean()`` / ``std()`` 等聚合方法将从头开始计算。

        ### 典型用法（流式场景）：
        ```python
        online_ewm = data.ewm(alpha=0.5).online()
        online_ewm.mean()           # 初次计算
        online_ewm.mean(update=new_data)  # 增量更新
        online_ewm.reset()          # 重置状态（如切换交易日/新批次）
        online_ewm.mean()           # 重新从 initial_data 开始
        ```
        """
        ...

    def aggregate(self, 
            func=None, 
            *args, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 在线 EWM 自定义聚合 —— 对在线指数加权窗口应用任意聚合函数

        支持传入自定义 Python 函数或字符串函数名，在在线 EWM 窗口上执行聚合操作。
        类似于 ``DataFrame.rolling().aggregate()``，但保持在线增量状态。

        Args:
            func: 聚合函数（可调用对象或字符串函数名），如 ``np.mean``、``"mean"``
            *args: 传递给 func 的位置参数
            **kwargs: 传递给 func 的关键字参数（含框架扩展参数如指标名称、绘图配置等）

        Returns:
            ``IndFrame`` 或 ``IndSeries``：框架自定义的指标数据类型
        """
        ...

    def std(self, 
            bias: bool = False, 
            *args, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 在线 EWM 标准差 —— 增量计算指数加权移动标准差

        Args:
            bias: 是否使用有偏估计（False = 无偏，使用贝塞尔校正；True = 有偏，除以 N）
            *args: 额外位置参数
            **kwargs: 框架扩展参数（如指标名称、绘图配置等）

        Returns:
            ``IndFrame`` 或 ``IndSeries``：框架自定义的指标数据类型
        """
        ...

    def corr(
        self,
        other: pd.DataFrame | pd.Series | None = None,
        pairwise: bool | None = None,
        numeric_only: bool = False,
        **kwargs
    ) -> IndFrame | IndSeries:
        """
        ## 在线 EWM 相关系数 —— 增量计算指数加权移动相关系数

        Args:
            other: 对比变量（默认 None，计算自身各列间的相关系数矩阵）
            pairwise: 是否两两计算；True 返回 MultiIndex DataFrame
            numeric_only: 是否仅包含数值列（默认 False）

        Returns:
            ``IndFrame`` 或 ``IndSeries``：框架自定义的指标数据类型
        """
        ...

    def cov(
        self,
        other: pd.DataFrame | pd.Series | None = None,
        pairwise: bool | None = None,
        bias: bool = False,
        numeric_only: bool = False,
        **kwargs
    ) -> IndFrame | IndSeries:
        """
        ## 在线 EWM 协方差 —— 增量计算指数加权移动协方差

        Args:
            other: 对比变量（默认 None，计算自身各列间的协方差矩阵）
            pairwise: 是否两两计算；True 返回 MultiIndex DataFrame
            bias: 是否使用有偏估计（False = 无偏，使用贝塞尔校正）
            numeric_only: 是否仅包含数值列（默认 False）

        Returns:
            ``IndFrame`` 或 ``IndSeries``：框架自定义的指标数据类型
        """
        ...

    def var(self, 
            bias: bool = False, 
            numeric_only: bool = False,
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 在线 EWM 方差 —— 增量计算指数加权移动方差

        Args:
            bias: 是否使用有偏估计（False = 无偏，使用贝塞尔校正；True = 有偏，除以 N）
            numeric_only: 是否仅包含数值列（默认 False）

        Returns:
            ``IndFrame`` 或 ``IndSeries``：框架自定义的指标数据类型
        """
        ...

    def mean(self, 
            *args, 
            update=None, 
            update_times=None, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 在线 EWM 均值 —— 增量计算指数加权移动均值

        这是在线 EWM 最核心的方法。首次调用时不传 ``update`` 参数（或传 ``None``），
        后续传入新数据切片（``update``）即可增量更新均值，无需从头重算全部历史。

        ### 在线更新机制：
        - **首次计算**：``online_ewm.mean()`` —— 对初始数据窗口计算 EWM 均值
        - **增量更新**：``online_ewm.mean(update=new_data)`` —— 继承上次权重，仅处理新增数据
        - **状态重置**：``online_ewm.reset()`` 后再调用 ``mean()``，等同于从头开始

        Args:
            *args: 额外位置参数
            update: 新增数据（DataFrame 或 Series），首次须为 ``None``
            update_times: 新数据的时间戳（暂不支持），默认 ``None`` 表示等间隔
            **kwargs: 框架扩展参数（如指标名称、绘图配置等）

        Returns:
            ``IndFrame`` 或 ``IndSeries``：框架自定义的指标数据类型

        Examples:
            >>> df = pd.DataFrame({"a": range(5), "b": range(5, 10)})
            >>> online_ewm = df.head(2).ewm(0.5).online()
            >>> online_ewm.mean()
                  a     b
            0  0.00  5.00
            1  0.75  5.75
            >>> online_ewm.mean(update=df.tail(3))
                      a         b
            2  1.615385  6.615385
            3  2.550000  7.550000
            4  3.520661  8.520661
            >>> online_ewm.reset()
            >>> online_ewm.mean()
                  a     b
            0  0.00  5.00
            1  0.75  5.75
        """
        ...


class BtExponentialMovingWindow(BtBaseWindows):
    """
    ## 量化回测框架中的指数加权移动窗口（Exponential Moving Window）增强类
    ### 核心功能：
    - 1. 封装 Pandas 的 ExponentialMovingWindow 功能，限制输入数据类型为框架支持的 IndSeries/IndFrame/Line
    - 2. 重写属性访问逻辑，将 Pandas EWM 原生方法（如 mean、std）的返回结果
       自动转换为框架内置的 IndSeries/IndFrame 类型（而非原生 Pandas 对象）
    - 3. 自动补充指标配置参数（如名称、计算设置），适配回测策略的指标管理体系
    - 4. 支持多种衰减参数（com、span、halflife、alpha），确保参数互斥性检查

    Args:
        obj: 输入数据对象（必须是框架自定义的 IndFrame/IndSeries/Line 类型，需包含 pandas_object 属性）
        com: 质心衰减参数（定义衰减方式，alpha = 1 / (1 + com)，与其他衰减参数互斥）
        span: 时间跨度衰减参数（大致对应窗口大小，alpha = 2 / (span + 1)，与其他衰减参数互斥）
        halflife: 半衰期衰减参数（权重减半所需时间，与其他衰减参数互斥）
        alpha: 直接平滑因子（值在0-1之间，与其他衰减参数互斥）
        min_periods: 窗口内最小非空值数量（默认0）
        adjust: 是否使用精确权重计算（默认True，False使用递归公式，金融场景推荐False）
        ignore_na: 是否忽略NaN值（默认False）
        axis: 计算轴方向（0为按行/时间维度，1为按列/特征维度，默认0）
        times: 时间序列（仅当halflife为时间类型时有效，默认None）
        method: 计算方法（默认"single"，预留参数用于扩展批量计算逻辑）
        selection: 选择特定列计算（默认None，表示所有列）
    """

    def __init__(
        self,
        obj: IndFrame | IndSeries | Line,
        com: float | None = None,
        span: float | None = None,
        halflife: float | timedelta | np.timedelta64 | np.int64 | float | str | None = None,
        alpha: float | None = None,
        min_periods: int | None = 0,
        adjust: bool = True,
        ignore_na: bool = False,
        axis: int | Literal["index", "columns", "rows"] = 0,
        times: np.ndarray | pd.Series | pd.DataFrame | None = None,
        method: str = "single",
        *,
        selection=None,
    ) -> None:
        """
        ## 初始化 BtExponentialMovingWindow 实例（指数加权移动窗口配置）

        Args:
            obj: 输入数据对象（必须是框架自定义的 IndFrame/IndSeries/Line 类型，需包含 pandas_object 属性）
            com: 质心衰减参数（定义衰减方式，alpha = 1 / (1 + com)，与其他衰减参数互斥）
            span: 时间跨度衰减参数（大致对应窗口大小，alpha = 2 / (span + 1)，与其他衰减参数互斥）
            halflife: 半衰期衰减参数（权重减半所需时间，与其他衰减参数互斥）
            alpha: 直接平滑因子（值在0-1之间，与其他衰减参数互斥）
            min_periods: 窗口内最小非空值数量（默认0）
            adjust: 是否使用精确权重计算（默认True，False使用递归公式，金融场景推荐False）
            ignore_na: 是否忽略NaN值（默认False）
            axis: 计算轴方向（0为按行/时间维度，1为按列/特征维度，默认0）
            times: 时间序列（仅当halflife为时间类型时有效，默认None）
            method: 计算方法（默认"single"，预留参数用于扩展批量计算逻辑）
            selection: 选择特定列计算（默认None，表示所有列）

        ### 关键逻辑：
        - 校验输入数据对象必须包含 'pandas_object' 属性
        - 检查衰减参数互斥性（com、span、halflife、alpha只能使用其中一个）
        - 调用父类 ExponentialMovingWindow 的初始化方法，传入原生 Pandas 对象和配置参数
        """
        super().__init__(obj, ewm_method)

        # 参数互斥性检查
        non_none_params = [p for p in [
            com, span, halflife, alpha] if p is not None]
        if len(non_none_params) > 1:
            raise ValueError("com、span、halflife 和 alpha 参数是互斥的，只能指定其中一个")

        # 如果没有指定任何衰减参数，使用默认的 com=0.5
        if len(non_none_params) == 0:
            com = 0.5  # pandas 的默认值

        # 调用父类 ExponentialMovingWindow 的构造方法，初始化指数加权移动窗口
        # pandas 3.0+ 不再支持 axis 参数
        ewm_kwargs = dict(
            com=com,
            span=span,
            halflife=halflife,
            alpha=alpha,
            min_periods=min_periods,
            adjust=adjust,
            ignore_na=ignore_na,
            times=times,
            method=method,
            selection=selection
        )
        if _PANDAS_VERSION < (3, 0):
            ewm_kwargs['axis'] = axis
        self._base_object = ExponentialMovingWindow(
            self._obj.pandas_object,
            **ewm_kwargs
        )


    def online(self, 
            engine: Literal["cython", "numba"] | None= "numba", 
            engine_kwargs:dict[str, bool] | None=None
    ) -> BtOnlineExponentialMovingWindow:
        """
        ## 获取在线指数加权移动窗口 —— 返回 ``BtOnlineExponentialMovingWindow`` 实例

        在线 EWM 允许流式/增量计算，适合实盘行情逐笔推送场景。首次调用聚合方法
        （如 ``mean()``）使用初始数据窗口，后续通过 ``update`` 参数追加新数据，
        而无需重算全部历史。

        ### 在线 vs 离线：
        - **离线 EWM**：``bt.ewm(alpha=0.5).mean()`` —— 对整个历史数据一次性计算
        - **在线 EWM**：``bt.ewm(alpha=0.5).online().mean()`` —— 可分批次增量计算，
          支持 ``.reset()`` 重置状态、``.mean(update=new_chunk)`` 流式追加

        Args:
            engine: 计算引擎，默认 ``"numba"``（JIT 加速）
            engine_kwargs: 引擎配置字典（如 ``{'nopython': True, 'nogil': False, 'parallel': False}``）

        Returns:
            ``BtOnlineExponentialMovingWindow``：框架封装的在线 EWM 窗口对象，
            支持 ``reset`` / ``mean`` / ``std`` / ``var`` / ``corr`` / ``cov`` / ``aggregate``

        .. Note::
            返回的 ``BtOnlineExponentialMovingWindow`` 继承自 ``BtBaseWindows``，
            其内部 ``_base_object`` 为 Pandas ``OnlineExponentialMovingWindow`` 对象。
        """
        return BtOnlineExponentialMovingWindow(
            obj=self.obj,
            com=self.com,
            span=self.span,
            halflife=self.halflife,
            alpha=self.alpha,
            min_periods=self.min_periods,
            adjust=self.adjust,
            ignore_na=self.ignore_na,
            times=self.times,
            engine=engine,
            engine_kwargs=engine_kwargs,
            selection=self._selection,
        )

    def aggregate(self, 
            func=None, 
            *args, 
            **kwargs)-> IndFrame | IndSeries:
        """
        ## 指数加权移动窗口自定义聚合 —— 对 EWM 窗口应用任意聚合函数

        支持传入自定义 Python 函数、字符串函数名、函数列表或字典，
        在指数加权移动窗口上执行批量或自定义聚合操作。``agg`` 是其别名。

        ### Pandas 3.0+ 新增方法
        早期的 Rolling/Expanding/EWM 中直接使用 ``apply``，Pandas 3.0 统一了
        聚合命名，将 ``aggregate`` / ``agg`` 设为标准聚合入口。

        Args:
            func: 聚合函数，支持以下形式：
                - 可调用对象（如 ``np.sum``）
                - 字符串函数名（如 ``"mean"``, ``"std"``）
                - 函数/函数名列表（如 ``[np.sum, 'mean']``）
                - 键为轴标签、值为函数/函数名列表的字典
            *args: 传递给 func 的位置参数
            **kwargs: 传递给 func 的关键字参数（含框架扩展参数）

        Returns:
            ``IndFrame`` 或 ``IndSeries``：框架自定义的指标数据类型

        Examples:
            >>> df = pd.DataFrame({"A": [1,2,3], "B": [4,5,6], "C": [7,8,9]})
            >>> df.ewm(alpha=0.5).aggregate(np.sum)
            >>> df.ewm(alpha=0.5).agg({"A": "sum", "B": "mean"})
        """
        ...

    agg = aggregate

    def mean(self, 
        numeric_only: bool = False, 
        engine:Literal["cython", "numba"] | None=None, 
        engine_kwargs:dict[str, bool] | None=None, 
        **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算指数加权移动平均（Exponentially Weighted Moving Average）
        Args:
            numeric_only: 是否仅对数值型列计算（默认False）
            engine: 计算引擎（默认None，用pandas原生引擎）
            engine_kwargs: 引擎参数（默认None）
            **kwargs: 框架扩展参数（如指标名称、绘图配置等）
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储指数加权移动平均值
        """
        ...

    def sum(self, 
            numeric_only: bool = False, 
            engine:Literal["cython", "numba"] | None=None, 
            engine_kwargs:dict[str, bool] | None=None, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算指数加权移动求和（Exponentially Weighted Moving Sum）
        Args:
            numeric_only: 是否仅对数值型列计算（默认False）
            engine: 计算引擎（默认None，用pandas原生引擎）
            engine_kwargs: 引擎参数（默认None）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储指数加权移动求和值
        """
        ...

    def std(self, 
            bias: bool = False, 
            numeric_only: bool = False, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算指数加权移动标准差（Exponentially Weighted Moving Standard Deviation）
        Args:
            bias: 是否使用有偏估计（默认False，使用无偏估计）
            numeric_only: 是否仅对数值型列计算（默认False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储指数加权移动标准差
        """
        ...

    def var(self, 
            bias: bool = False, 
            numeric_only: bool = False, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算指数加权移动方差（Exponentially Weighted Moving Variance）
        Args:
            bias: 是否使用有偏估计（默认False，使用无偏估计）
            numeric_only: 是否仅对数值型列计算（默认False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储指数加权移动方差
        """
        ...

    def cov(self, 
            other: pd.DataFrame | pd.Series | None = None, 
            pairwise: bool | None = None,
            bias: bool = False, 
            numeric_only: bool = False, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算指数加权移动协方差（Exponentially Weighted Moving Covariance）
        Args:
            other: 对比变量（默认None，计算自身协方差）
            pairwise: 是否两两计算协方差（默认None，跟随pandas规则）
            bias: 是否使用有偏估计（默认False，使用无偏估计）
            numeric_only: 是否仅对数值型列计算（默认False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储指数加权移动协方差
        """
        ...

    def corr(self, 
            other: pd.DataFrame | pd.Series | None = None, 
            pairwise: bool | None = None,
            numeric_only: bool = False, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算指数加权移动相关系数（Exponentially Weighted Moving Correlation）
        Args:
            other: 对比变量（默认None，计算自身相关）
            pairwise: 是否两两计算相关系数（默认None，跟随pandas规则）
            numeric_only: 是否仅对数值型列计算（默认False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储指数加权移动相关系数
        """
        ...


class BtExponentialMovingWindowGroupby(BtExponentialMovingWindow):
    """
    ## 量化回测框架中的分组指数加权移动窗口（ExponentialMovingWindow Groupby）增强类

    - 继承自 ``BtExponentialMovingWindow``，在指数加权移动窗口的基础上增加分组（Groupby）能力
    - 每个分组内独立进行指数加权计算，分组间互不干扰

    ### 核心功能：
    - 1. 支持按指定列（或列组合）对数据进行分组后，在组内做指数加权移动统计计算
    - 2. 内部构造 pandas ``ExponentialMovingWindowGroupby`` 对象，利用 ``BaseGrouper`` 管理分组索引
    - 3. 通过父类 ``BtBaseWindows`` 的装饰器机制，将返回结果自动转换为框架内置的 IndSeries / IndFrame 类型
    - 4. 保留 ``BtExponentialMovingWindow`` 的全部方法（mean、std、var、corr、cov、aggregate 等），每个方法按组独立计算
    - 5. 支持 ``.online()`` 获取在线增量计算模式（每组独立维护 EWM 状态）

    ### 设计模式：
    - 适配器模式：封装 pandas ``ExponentialMovingWindowGroupby`` 构造流程
    - 装饰器模式：继承父类 ``__getattribute__`` 拦截逻辑，自动包装窗口方法

    ### 继承关系：
        BtBaseWindows → BtExponentialMovingWindow → BtExponentialMovingWindowGroupby

    ### 使用场景：
    - 不同行业 / 板块的分组 EMA 计算（按 sector 分组）
    - 多品种分组的指数加权波动率统计
    - 分组内 MACD 等衍生指标的批量计算

    Examples:
        >>> # 按 sector 分组，计算每组的 EMA12
        >>> eg = BtExponentialMovingWindowGroupby(df, by="sector", span=12, adjust=False)
        >>> eg.mean()  # 每组独立的 EMA

        >>> # 按多列分组，计算指数加权标准差
        >>> eg = BtExponentialMovingWindowGroupby(df, by=["sector", "exchange"], alpha=0.5)
        >>> eg.std()
    """

    # _base_object 实际为 ExponentialMovingWindowGroupby 类型（继承链 BaseWindowGroupby + ExponentialMovingWindow）
    # BtBaseWindows._base_object 注解为 Rolling | ExponentialMovingWindow | Expanding，
    # ExponentialMovingWindowGroupby 是 ExponentialMovingWindow 的子类，因此类型兼容
    _base_object: ExponentialMovingWindowGroupby
    """底层 pandas ExponentialMovingWindowGroupby 对象，按组执行指数加权移动窗口计算"""

    def __init__(
        self,
        obj: IndFrame | IndSeries | Line,
        by,
        com: float | None = None,
        span: float | None = None,
        halflife: float | timedelta | np.timedelta64 | np.int64 | str | None = None,
        alpha: float | None = None,
        min_periods: int = 0,
        adjust: bool = True,
        ignore_na: bool = False,
        axis: int | Literal["index", "columns", "rows"] = 0,
        times: np.ndarray | pd.Series | pd.DataFrame | None = None,
        method: str = "single",
        *,
        selection=None,
    ) -> None:
        """
        ## 初始化 BtExponentialMovingWindowGroupby 实例（分组指数加权移动窗口配置）

        内部通过 ``pandas.DataFrame.groupby(by)`` 获取分组器（``BaseGrouper``），
        再将其传入 ``ExponentialMovingWindowGroupby`` 构造器，使每个分组独立维护自己的指数加权移动窗口。

        ### 与 BtExponentialMovingWindow 的关键差异：
        - 新增 ``by`` 参数指定分组列
        - 内部不直接构造 ``ExponentialMovingWindow(obj)``，而是构造 ``ExponentialMovingWindowGroupby(groupby._selected_obj, _grouper=...)``
        - ``groupby._selected_obj`` 已自动去除分组列，避免分组列参与数值计算
        - 当 ``times`` 参数不为空时，pandas 内部会按分组顺序重算时间增量（``_deltas``），确保基于时间的 halflife 计算正确

        ### 实现流程：
        1. 调用父类 ``BtExponentialMovingWindow.__init__`` 完成基础窗口配置（``_obj``、参数互斥校验等）
        2. 对 ``_obj.pandas_object`` 执行 ``groupby(by)``，获取 ``_grouper`` 和 ``_selected_obj``
        3. 将 ``_selected_obj`` 作为数据，``_grouper`` 作为分组器，构造 ``ExponentialMovingWindowGroupby``
        4. pandas 底层自动使用 ``GroupbyIndexer`` 包裹 ``ExponentialMovingWindowIndexer``，实现组内独立指数加权

        Args:
            obj: 输入数据对象（必须是框架自定义的 IndFrame / IndSeries / Line 类型，需包含 pandas_object 属性）
            by: 分组依据，遵循 pandas ``DataFrame.groupby(by)`` 的参数规范：
                - ``str``：单个列名（如 ``"sector"``）
                - ``list[str]``：多个列名（如 ``["sector", "exchange"]``）
                - ``pd.Series``：自定义分组序列
                - 其他 pandas groupby 支持的 ``by`` 参数形式
            com: 质心衰减参数（定义衰减方式，``α = 1 / (1 + com)``，与其他衰减参数互斥）
            span: 时间跨度衰减参数（大致对应窗口大小，``α = 2 / (span + 1)``，与其他衰减参数互斥）
            halflife: 半衰期衰减参数（权重减半所需时间，可传入 ``timedelta``, ``np.timedelta64`` 等时间类型）
            alpha: 直接平滑因子（值在 0-1 之间，与其他衰减参数互斥）
            min_periods: 窗口内最小非空值数量（默认 0）
            adjust: 是否使用精确权重计算（默认 True，False 使用递归公式，金融场景推荐 False）
            ignore_na: 是否忽略 NaN 值（默认 False）
            axis: 计算轴方向（0 为按行 / 时间维度，1 为按列 / 特征维度，默认 0）
                pandas 3.0+ 不再支持 axis 参数，框架内部自动兼容
            times: 时间序列（仅当 ``halflife`` 为时间类型时有效，默认 None）
            method: 计算方法（默认 ``"single"``，预留参数用于扩展批量计算逻辑）
            selection: 选择特定列计算（默认 None，表示所有数值列）

        ### 关键逻辑：
        - 衰减参数互斥性检查（com、span、halflife、alpha 只能指定一个，由父类完成）
        - 自动从 ``pandas_object`` 中去除分组列，避免分组列参与数值计算
        - 每个分组独立维护指数加权移动窗口，分组间互不干扰
        - 继承 ``BtExponentialMovingWindow`` 的全部方法集，支持 ``.online()`` 转换

        Raises:
            ValueError: 当多个衰减参数同时指定时（由父类检查抛出）
            ValueError: 当 ``by`` 参数无法用于构造有效的分组器时（由 pandas ``groupby()`` 内部抛出）

        Examples:
            >>> # 按 sector 列分组，EMA12
            >>> eg = BtExponentialMovingWindowGroupby(df, by="sector", span=12, adjust=False)
            >>> eg.mean()
            >>>
            >>> # 按多列分组 + 指数加权标准差
            >>> eg = BtExponentialMovingWindowGroupby(df, by=["sector", "exchange"], alpha=0.5)
            >>> eg.std()
            >>>
            >>> # 只对 close 列做分组 EWM
            >>> eg = BtExponentialMovingWindowGroupby(df, by="symbol", span=20, selection="close")
            >>> eg.mean()
        """
        # 1. 调用父类 BtExponentialMovingWindow.__init__ → BtBaseWindows.__init__
        #    完成 _obj 赋值、参数互斥校验、衰减参数默认值设置
        #    注意：必须显式传参，不可将 ewm_method（set）作为其他参数误传
        super().__init__(
            obj,
            com=com,
            span=span,
            halflife=halflife,
            alpha=alpha,
            min_periods=min_periods,
            adjust=adjust,
            ignore_na=ignore_na,
            axis=axis,
            times=times,
            method=method,
            selection=selection,
        )
        # 父类 BtExponentialMovingWindow.__init__ 内部已创建
        # self._base_object = ExponentialMovingWindow(...)，
        # 下面将用 ExponentialMovingWindowGroupby 覆盖之，这是有意为之的设计

        # 2. 对原生 pandas 对象执行 groupby，获取分组器 _grouper 和去分组列后的数据 _selected_obj
        #    groupby 内部通过 BaseGrouper 管理分组索引，
        #    ExponentialMovingWindowGroupby 会使用它来按组计算
        groupby_obj = self._obj.pandas_object.groupby(by)

        # 3. 构建 ExponentialMovingWindowGroupby 所需的参数
        #    - _grouper: 必须参数，BaseGrouper 对象，管理分组索引
        #    - _as_index: 是否将分组列作为结果索引（默认 True，与 pandas 行为一致）
        #    其余 EWM 参数（com, span, halflife, alpha, ...）通过 **ewm_kwargs 传入
        ewm_kwargs = dict(
            com=com,
            span=span,
            halflife=halflife,
            alpha=alpha,
            min_periods=min_periods,
            adjust=adjust,
            ignore_na=ignore_na,
            times=times,
            method=method,
            selection=selection,
            _grouper=groupby_obj._grouper,
            _as_index=True,
        )
        if _PANDAS_VERSION < (3, 0):
            ewm_kwargs['axis'] = axis

        # 4. 使用 groupby._selected_obj（已丢弃分组列的数据）构造 ExponentialMovingWindowGroupby
        #    BaseWindowGroupby.__init__ 内部会再次 drop 分组列，此为 pandas 自身的安全机制
        self._base_object = ExponentialMovingWindowGroupby(
            groupby_obj._selected_obj,
            **ewm_kwargs
        )


class BtExpanding(BtBaseWindows):
    """
    ## 量化回测框架中的扩展窗口（Expanding Window）增强类
    ### 核心功能：
    1. 封装 Pandas 的 Expanding 功能，限制输入数据类型为框架支持的 IndSeries/IndFrame/Line
    2. 重写属性访问逻辑，将 Pandas Expanding 原生方法（如 mean、std）的返回结果
       自动转换为框架内置的 IndSeries/IndFrame 类型（而非原生 Pandas 对象）
    3. 自动补充指标配置参数（如名称、计算设置），适配回测策略的指标管理体系
    4. 支持从数据起始点到当前点的累积统计计算，常用于计算全样本统计量

    Args:
        obj: 输入数据对象（必须是框架自定义的 IndFrame/IndSeries/Line 类型，需包含 pandas_object 属性）
        min_periods: 窗口内最小非空值数量（默认1，即从第1个数据点开始计算）
        axis: 计算轴方向（0为按行/时间维度，1为按列/特征维度，默认0）
        method: 计算方法（默认"single"，预留参数用于扩展批量计算逻辑）
        selection: 选择特定列计算（默认None，表示所有列）
    """

    def __init__(
        self,
        obj: IndFrame | IndSeries | Line,
        min_periods: int = 1,
        axis: int | Literal["index", "columns", "rows"] = 0,
        method: str = "single",
        selection=None,
    ) -> None:
        """
        ## 初始化 BtExpanding 实例（扩展窗口配置）

        Args:
            obj: 输入数据对象（必须是框架自定义的 IndFrame/IndSeries/Line 类型，需包含 pandas_object 属性）
            min_periods: 窗口内最小非空值数量（默认1，即从第1个数据点开始计算）
            axis: 计算轴方向（0为按行/时间维度，1为按列/特征维度，默认0）
            method: 计算方法（默认"single"，预留参数用于扩展批量计算逻辑）
            selection: 选择特定列计算（默认None，表示所有列）

        ### 关键逻辑：
        - 校验输入数据对象必须包含 'pandas_object' 属性
        - 调用父类 Expanding 的初始化方法，传入原生 Pandas 对象和扩展窗口参数
        - 扩展窗口从数据起始点开始，逐步包含更多数据点，计算累积统计量
        """
        super().__init__(obj, expanding_method)

        # 调用父类 Expanding 的构造方法，初始化扩展窗口（传入原生 Pandas 对象和配置参数）
        # pandas 3.0+ 不再支持 axis 参数
        expanding_kwargs = dict(
            min_periods=min_periods,
            method=method,
            selection=selection
        )
        if _PANDAS_VERSION < (3, 0):
            expanding_kwargs['axis'] = axis
        self._base_object = Expanding(
            self._obj.pandas_object,
            **expanding_kwargs
        )

    def aggregate(self, 
            func=None, 
            *args, 
            **kwargs)-> IndFrame | IndSeries:
        """
        ## 扩展窗口自定义聚合 —— 对累积窗口应用任意聚合函数

        支持传入自定义 Python 函数、字符串函数名、函数列表或字典，
        在扩展窗口（从起始点到当前点）上执行批量或自定义聚合操作。
        ``agg`` 是其别名。

        ### Pandas 3.0+ 新增方法
        统一了聚合入口命名，使 Rolling / Expanding / EWM 三种窗口的聚合 API 保持一致。

        Args:
            func: 聚合函数，支持以下形式：
                - 可调用对象（如 ``np.sum``）
                - 字符串函数名（如 ``"mean"``, ``"std"``）
                - 函数/函数名列表（如 ``[np.sum, 'mean']``）
                - 键为列名、值为函数的字典（如 ``{"A": "sum", "B": "min"}``）
            *args: 传递给 func 的位置参数
            **kwargs: 传递给 func 的关键字参数（含框架扩展参数如指标名称、绘图配置等）

        Returns:
            ``IndFrame`` 或 ``IndSeries``：框架自定义的指标数据类型

        Examples:
            >>> df.expanding(2).aggregate({"A": "sum", "B": "min"})
        """
        ...

    agg = aggregate

    def count(self, numeric_only: bool = False, **kwargs) -> IndFrame | IndSeries:
        """
        ## 统计扩展窗口内的非空值数量（从起始点到当前点的累积计数）
        Args:
            numeric_only: 是否仅统计数值型列（默认False）
            **kwargs: 框架扩展参数（如指标名称、绘图配置等）
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储累积非空值数量
        """
        ...

    def apply(self, 
            func: Callable[..., Any], 
            raw: bool = False,
            engine: Literal["cython", "numba"] | None = None,
            engine_kwargs: dict[str, bool] | None = None,
            args: tuple[Any, ...] | None = None, 
            **kwargs: dict[str, Any]) -> IndFrame | IndSeries:
        """
        ## 对扩展窗口内的数据应用自定义函数（支持累积计算逻辑）
        Args:
            func: 自定义函数（输入为窗口数据，返回计算结果）
            raw: 是否传入原始数组（默认False，传入Series/DataFrame）
            engine: 计算引擎（默认None，用pandas原生引擎；"cython"或"numba"用于性能优化）
            engine_kwargs: 引擎参数（默认None）
            args: 函数额外参数（默认None）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储自定义函数的累积计算结果
        """
        ...

    def pipe(
        self,
        func: Callable[Concatenate[BtExpanding, ParamSpec['P']], T] | tuple[Callable[..., T], str],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """
        ## 管道操作 —— 将扩展窗口对象传入函数并返回结果

        ``pipe`` 是函数式编程风格的链式调用入口。它把当前 ``Expanding`` 对象作为
        第一个参数传给 ``func``，使嵌套函数调用变为可读的链式写法。

        ### Pandas 3.0+ 新增方法
        与 DataFrame.pipe / Series.pipe 保持一致的管道式 API。

        ### 适用场景：
        - 在指标计算链中插入自定义处理逻辑（如归一化、去噪）
        - 提高多重嵌套函数调用的可读性

        ```python
        # 嵌套写法（可读性差）
        h(g(f(data.expanding()), arg1=1), arg2=2, arg3=3)

        # 管道写法（清晰直观）
        data.expanding().pipe(f).pipe(g, arg1=1).pipe(h, arg2=2, arg3=3)
        ```

        Args:
            func: 待应用的函数，或 ``(callable, data_keyword)`` 元组
            *args: 传递给 func 的位置参数
            **kwargs: 传递给 func 的关键字参数

        Returns:
            函数 ``func`` 的返回值（类型取决于 func 的返回类型）

        Examples:
            >>> df.expanding().pipe(lambda x: x.max() - x.min())
                      A
            2012-08-02  0.0
            2012-08-03  1.0
            2012-08-04  2.0
            2012-08-05  3.0
        """
        ...

    def sum(self, 
            numeric_only: bool = False,
            engine: Literal["cython", "numba"] | None = None,
            engine_kwargs: dict[str, bool] | None = None, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算扩展窗口内的累积和（从起始点到当前点的累加值）
        Args:
            numeric_only: 是否仅对数值型列计算（默认False）
            engine: 计算引擎（默认None，用pandas原生引擎）
            engine_kwargs: 引擎参数（默认None）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储累积和
        """
        ...

    def max(self, 
            numeric_only: bool = False,
            engine: Literal["cython", "numba"] | None = None,
            engine_kwargs: dict[str, bool] | None = None, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算扩展窗口内的累积最大值（从起始点到当前点的历史最大值）
        Args:
            numeric_only: 是否仅对数值型列计算（默认False）
            engine: 计算引擎（默认None，用pandas原生引擎）
            engine_kwargs: 引擎参数（默认None）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储累积最大值
        """
        ...

    def min(self, 
            numeric_only: bool = False,
            engine: Literal["cython", "numba"] | None = None,
            engine_kwargs: dict[str, bool] | None = None, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算扩展窗口内的累积最小值（从起始点到当前点的历史最小值）
        Args:
            numeric_only: 是否仅对数值型列计算（默认False）
            engine: 计算引擎（默认None，用pandas原生引擎）
            engine_kwargs: 引擎参数（默认None）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储累积最小值
        """
        ...

    def mean(self, 
            numeric_only: bool = False,
            engine: Literal["cython", "numba"] | None = None,
            engine_kwargs: dict[str, bool] | None = None, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算扩展窗口内的累积均值（从起始点到当前点的移动平均值）
        Args:
            numeric_only: 是否仅对数值型列计算（默认False）
            engine: 计算引擎（默认None，用pandas原生引擎）
            engine_kwargs: 引擎参数（默认None）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储累积均值
        """
        ...

    def median(self, 
            numeric_only: bool = False,
            engine: Literal["cython", "numba"] | None = None,
            engine_kwargs: dict[str, bool] | None = None, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算扩展窗口内的累积中位数（从起始点到当前点的移动中位数）
        Args:
            numeric_only: 是否仅对数值型列计算（默认False）
            engine: 计算引擎（默认None，用pandas原生引擎）
            engine_kwargs: 引擎参数（默认None）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储累积中位数
        """
        ...

    def std(self, 
            ddof: int = 1, 
            numeric_only: bool = False,
            engine: Literal["cython", "numba"] | None = None,
            engine_kwargs: dict[str, bool] | None = None, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算扩展窗口内的累积标准差（从起始点到当前点的移动标准差）
        Args:
            ddof: 自由度（默认1，样本标准差）
            numeric_only: 是否仅对数值型列计算（默认False）
            engine: 计算引擎（默认None，用pandas原生引擎）
            engine_kwargs: 引擎参数（默认None）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储累积标准差
        """
        ...

    def var(self, 
            ddof: int = 1, 
            numeric_only: bool = False,
            engine: Literal["cython", "numba"] | None = None,
            engine_kwargs: dict[str, bool] | None = None, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算扩展窗口内的累积方差（从起始点到当前点的移动方差）
        Args:
            ddof: 自由度（默认1，样本方差）
            numeric_only: 是否仅对数值型列计算（默认False）
            engine: 计算引擎（默认None，用pandas原生引擎）
            engine_kwargs: 引擎参数（默认None）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储累积方差
        """
        ...

    def sem(self, 
            ddof: int = 1, 
            numeric_only: bool = False, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算扩展窗口内的累积标准误（均值的标准偏差，反映均值的抽样误差）
        Args:
            ddof: 自由度（默认1）
            numeric_only: 是否仅对数值型列计算（默认False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储累积标准误
        """
        ...

    def skew(self, 
            numeric_only: bool = False, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算扩展窗口内的累积偏度（衡量数据分布不对称性的变化）
        Args:
            numeric_only: 是否仅对数值型列计算（默认False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储累积偏度
        """
        ...

    def kurt(self, 
            numeric_only: bool = False, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算扩展窗口内的累积峰度（衡量数据分布陡峭程度的变化）
        Args:
            numeric_only: 是否仅对数值型列计算（默认False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储累积峰度
        """
        ...

    def first(self, 
            numeric_only: bool = False,
            **kwargs)-> IndFrame | IndSeries:
        """
        ## 扩展窗口首元素 —— 返回每个扩展窗口中最左侧（最早）的元素

        对每个时间点，返回从起始点到该点之间窗口中第一个有效元素的值。
        要求窗口满足 ``min_periods`` 后方才开始计算，之前返回 NaN。

        ### Pandas 3.0+ 新增方法

        Args:
            numeric_only: 是否仅包含数值列（默认 False）

        Returns:
            ``IndFrame`` 或 ``IndSeries``：每个时间点对应的窗口首元素值

        Examples:
            >>> s = pd.Series(range(5))
            >>> s.expanding(3).first()
            0   NaN
            1   NaN
            2   0.0
            3   0.0
            4   0.0
        """
        ...

    def last(self, 
            numeric_only: bool = False,
            **kwargs)-> IndFrame | IndSeries:
        """
        ## 扩展窗口末元素 —— 返回每个扩展窗口中最右侧（最新）的元素

        对每个时间点，返回从起始点到该点之间窗口中最后一个有效元素的值。
        要求窗口满足 ``min_periods`` 后方才计算，之前返回 NaN。

        ### Pandas 3.0+ 新增方法

        Args:
            numeric_only: 是否仅包含数值列（默认 False）

        Returns:
            ``IndFrame`` 或 ``IndSeries``：每个时间点对应的窗口末元素（即当前值）

        Examples:
            >>> s = pd.Series(range(5))
            >>> s.expanding(3).last()
            0   NaN
            1   NaN
            2   2.0
            3   3.0
            4   4.0
        """
        ...

    def quantile(self, q: float, 
                interpolation: Literal["linear", "lower", "higher", "midpoint", "nearest"] = "linear",
                numeric_only: bool = False, **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算扩展窗口内的累积分位数（从起始点到当前点的移动分位数）
        Args:
            q: 分位数（0-1之间，如0.5为中位数）
            interpolation: 插值方式（默认"linear"，处理分位数落在两数之间的情况）
            numeric_only: 是否仅对数值型列计算（默认False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储累积分位数
        """
        ...

    def rank(self, 
            method: Literal["average", "min", "max"] = "average",
            ascending: bool = True, 
            pct: bool = False, 
            numeric_only: bool = False, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算扩展窗口内的累积排名（对从起始点到当前点的数据进行排序分配名次）
        Args:
            method: 排名方式（默认"average"，相同值取平均名次）
            ascending: 是否升序（默认True，小值排名靠前）
            pct: 是否返回百分比排名（默认False）
            numeric_only: 是否仅对数值型列计算（默认False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储累积排名
        """
        ...
    
    def nunique(
        self,
        numeric_only: bool = False,
        **kwargs
    )-> IndFrame | IndSeries:
        """
        ## 扩展窗口唯一值计数 —— 累积统计窗口中不同值的个数

        对每个时间点，统计从起始点到该点的窗口中出现了多少个不同的值。

        ### Pandas 3.0.0 新增方法

        Args:
            numeric_only: 是否仅包含数值列（默认 False）
            **kwargs: 框架扩展参数（如指标名称、绘图配置等）

        Returns:
            ``IndFrame`` 或 ``IndSeries``：每个时间点对应的累积唯一值数量

        Examples:
            >>> s = pd.Series([1, 4, 2, 3, 5, 3])
            >>> s.expanding().nunique()
            0    1.0
            1    2.0
            2    3.0
            3    4.0
            4    5.0
            5    5.0
        """
        ...

    def cov(self, other: pd.DataFrame | pd.Series | None = None, 
            pairwise: bool | None = None,
            ddof: int = 1, 
            numeric_only: bool = False, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算扩展窗口内的累积协方差（从起始点到当前点的移动协方差）
        Args:
            other: 对比变量（默认None，计算自身协方差）
            pairwise: 是否两两计算协方差（默认None，跟随pandas规则）
            ddof: 自由度（默认1，样本协方差）
            numeric_only: 是否仅对数值型列计算（默认False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储累积协方差
        """
        ...

    def corr(self, 
            other: pd.DataFrame | pd.Series | None = None, 
            pairwise: bool | None = None,
            ddof: int = 1, 
            numeric_only: bool = False, 
            **kwargs) -> IndFrame | IndSeries:
        """
        ## 计算扩展窗口内的累积相关系数（从起始点到当前点的移动相关系数）
        Args:
            other: 对比变量（默认None，计算自身相关）
            pairwise: 是否两两计算相关系数（默认None，跟随pandas规则）
            ddof: 自由度（默认1，样本相关系数）
            numeric_only: 是否仅对数值型列计算（默认False）
            **kwargs: 框架扩展参数
        Returns:
            框架自定义的IndFrame/IndSeries类型结果，存储累积相关系数
        """
        ...


class BtExpandingGroupby(BtExpanding):
    """
    ## 量化回测框架中的分组扩展窗口（Expanding Groupby）增强类

    - 继承自 ``BtExpanding``，在扩展窗口的基础上增加分组（Groupby）能力
    - 每个分组内独立进行扩展窗口计算，分组间互不干扰

    ### 核心功能：
    - 1. 支持按指定列（或列组合）对数据进行分组后，在组内做累积统计计算
    - 2. 内部构造 pandas ``ExpandingGroupby`` 对象，利用 ``BaseGrouper`` 管理分组索引
    - 3. 通过父类 ``BtBaseWindows`` 的装饰器机制，将返回结果自动转换为框架内置的 IndSeries / IndFrame 类型
    - 4. 保留 ``BtExpanding`` 的全部方法（mean、std、sum、aggregate 等），每个方法按组独立计算

    ### 设计模式：
    - 适配器模式：封装 pandas ``ExpandingGroupby`` 构造流程
    - 装饰器模式：继承父类 ``__getattribute__`` 拦截逻辑，自动包装窗口方法

    ### 继承关系：
        BtBaseWindows → BtExpanding → BtExpandingGroupby

    ### 使用场景：
    - 不同行业/板块的累积收益率计算（按 sector 分组）
    - 多品种分组的累积波动率统计
    - 分组内排名指标的累积计算

    Examples:
        >>> # 按 sector 分组，计算每组的累积均值和标准差
        >>> eg = BtExpandingGroupby(df, by="sector")
        >>> eg.mean()  # 每组独立的 expanding mean
        >>> eg.std()   # 每组独立的 expanding std

        >>> # 按多列分组
        >>> eg = BtExpandingGroupby(df, by=["sector", "exchange"])
        >>> eg.sum()
    """

    # _base_object 实际为 ExpandingGroupby 类型（继承链 BaseWindowGroupby + Expanding）
    # BtBaseWindows._base_object 注解为 Rolling | ExponentialMovingWindow | Expanding，
    # ExpandingGroupby 是 Expanding 的子类，因此类型兼容
    _base_object: ExpandingGroupby
    """底层 pandas ExpandingGroupby 对象，按组执行扩展窗口计算"""

    def __init__(
        self,
        obj: IndFrame | IndSeries | Line,
        by,
        min_periods: int = 1,
        axis: int | Literal["index", "columns", "rows"] = 0,
        method: str = "single",
        selection=None,
    ) -> None:
        """
        ## 初始化 BtExpandingGroupby 实例（分组扩展窗口配置）

        内部通过 ``pandas.DataFrame.groupby(by)`` 获取分组器（``BaseGrouper``），
        再将其传入 ``ExpandingGroupby`` 构造器，使每个分组独立维护自己的扩展窗口。

        ### 与 BtExpanding 的关键差异：
        - 新增 ``by`` 参数指定分组列
        - 内部不直接构造 ``Expanding(obj)``，而是构造 ``ExpandingGroupby(groupby._selected_obj, _grouper=...)``
        - ``groupby._selected_obj`` 已自动去除分组列，避免分组列参与窗口计算

        ### 实现流程：
        1. 调用父类 ``BtExpanding.__init__`` 完成基础窗口配置（``_obj``、``_method`` 等）
        2. 对 ``_obj.pandas_object`` 执行 ``groupby(by)``，获取 ``_grouper`` 和 ``_selected_obj``
        3. 将 ``_selected_obj`` 作为数据，``_grouper`` 作为分组器，构造 ``ExpandingGroupby``
        4. pandas 底层自动使用 ``GroupbyIndexer`` 包裹 ``ExpandingIndexer``，实现组内独立扩展

        Args:
            obj: 输入数据对象（必须是框架自定义的 IndFrame / IndSeries / Line 类型，需包含 pandas_object 属性）
            by: 分组依据，遵循 pandas ``DataFrame.groupby(by)`` 的参数规范：
                - ``str``：单个列名（如 ``"sector"``）
                - ``list[str]``：多个列名（如 ``["sector", "exchange"]``）
                - ``pd.Series``：自定义分组序列
                - 其他 pandas groupby 支持的 ``by`` 参数形式
            min_periods: 窗口内最小非空值数量（默认 1，即从每组第 1 个数据点就开始计算）
            axis: 计算轴方向（0 为按行 / 时间维度，1 为按列 / 特征维度，默认 0）
                pandas 3.0+ 不再支持 axis 参数，框架内部自动兼容
            method: 计算方法（默认 ``"single"``，预留参数用于扩展批量计算逻辑）
            selection: 选择特定列计算（默认 None，表示所有数值列）

        ### 关键逻辑：
        - 校验输入数据对象必须包含 ``'pandas_object'`` 属性（由父类 ``BtBaseWindows.__init__`` 完成）
        - 自动从 ``pandas_object`` 中去除分组列，避免分组列参与数值计算
        - 每个分组独立维护扩展窗口（起始点 → 当前点），分组间互不干扰
        - 继承 ``BtExpanding`` 的全部方法集，无需重复定义

        Raises:
            ValueError: 当 ``by`` 参数无法用于构造有效的分组器时（由 pandas ``groupby()`` 和 ``ExpandingGroupby`` 内部抛出）

        Examples:
            >>> # 按 sector 列分组，累积均值
            >>> eg = BtExpandingGroupby(df, by="sector", min_periods=3)
            >>> eg.mean()
            >>>
            >>> # 按多列分组 + 指定计算列
            >>> eg = BtExpandingGroupby(df, by=["sector", "exchange"], selection="close")
            >>> eg.std()
        """
        # 1. 调用父类 BtExpanding.__init__ → BtBaseWindows.__init__
        #    完成 _obj 赋值、_method 设置、pandas_object 属性校验
        #    注意：必须显式传参，不可将 expanding_method（set）作为 min_periods（int）误传
        super().__init__(
            obj,
            min_periods=min_periods,
            axis=axis,
            method=method,
            selection=selection,
        )
        # 父类 BtExpanding.__init__ 内部已创建 self._base_object = Expanding(...)，
        # 下面将用 ExpandingGroupby 覆盖之，这是有意为之的设计

        # 2. 对原生 pandas 对象执行 groupby，获取分组器 _grouper 和去分组列后的数据 _selected_obj
        #    groupby 内部通过 BaseGrouper 管理分组索引，ExpandingGroupby 会使用它来按组计算
        groupby_obj = self._obj.pandas_object.groupby(by)

        # 3. 构建 ExpandingGroupby 所需的参数
        #    - _grouper: 必须参数，BaseGrouper 对象，管理分组索引
        #    - _as_index: 是否将分组列作为结果索引（默认 True，与 pandas 行为一致）
        expanding_kwargs = dict(
            min_periods=min_periods,
            method=method,
            selection=selection,
            _grouper=groupby_obj._grouper,
            _as_index=True,
        )
        if _PANDAS_VERSION < (3, 0):
            expanding_kwargs['axis'] = axis

        # 4. 使用 groupby._selected_obj（已丢弃分组列的数据）构造 ExpandingGroupby
        #    BaseWindowGroupby.__init__ 内部会再次 drop 分组列，此为 pandas 自身的安全机制
        self._base_object = ExpandingGroupby(
            groupby_obj._selected_obj,
            **expanding_kwargs
        )

@dataclass
class KLineSetting(DataSetBase):
    """
    ## K线设置数据类 - 存储K线相关配置和状态信息

    - 继承自DataSetBase，用于管理单个交易品种的K线数据和相关交易设置

    ### 属性说明：
    ```python
    symbol_info : SymbolInfo
        交易品种信息对象，包含品种代码、交易规则等

    current_open : np.ndarray
        当前K线的开盘价数组，用于技术指标计算和交易信号判断

    current_close : np.ndarray
        当前K线的收盘价数组，用于技术指标计算和交易信号判断

    current_datetime : np.ndarray[str]
        当前K线的时间戳数组，与current_close一一对应

    current_time : np.ndarray[datetime.datetime]
        当前K线的时间戳数组，与current_close一一对应

    isstop : bool = False
        是否已触发止损/止盈停止交易标志
        - True: 已停止交易
        - False: 正常交易中（默认）

    stop : Stop | None = None
        止损/止盈设置对象，包含止损价、止盈价等参数

    stop_lines : IndFrame | None = None
        止损/止盈线指标框架，用于在图表上显示止损止盈线
        - 包含'stop_price'(止损价)和'target_price'(止盈价)两条线

    tradable : bool = True
        当前品种是否可交易标志
        - True: 可交易（默认）
        - False: 不可交易（如停牌、非交易时段等）

    istrader : bool = False
        是否为交易者模式标志
        - True: 交易者模式（产生实际交易信号）
        - False: 观察者模式（默认）

    follow : bool = True
        是否跟随主图标志（多周期图表同步）
        - True: 跟随主图周期（默认）
        - False: 独立显示

    plot_index : list[int] | None = None
        绘图索引列表，指定在图表中的显示位置
        - None: 自动分配位置
        - [int]: 自定义显示索引
    ```

    ### 方法说明：
    >>> set_default_stop_lines(id: BtID):
    设置默认的止损止盈线
    - 创建包含止损价和止盈价两条线的IndFrame对象
    - 设置线型样式为SpanList（区间样式）
    - 与主图叠加显示
    """

    symbol_info: SymbolInfo  # 交易品种信息
    current_open: np.ndarray  # 当前开盘价数组
    current_high: np.ndarray  # 当前开盘价数组
    current_low: np.ndarray  # 当前收盘价数组
    current_close: np.ndarray  # 当前收盘价数组
    current_datetime: np.ndarray  # 当前时间戳数组(str)
    current_time: np.ndarray  # 当前时间戳数组(datetime.datetime)
    isstop: bool = False  # 是否停止交易标志
    stop: Stop | None = None  # 止损/止盈设置
    stop_lines: IndFrame | None = None  # 止损止盈线指标
    tradable: bool = True  # 是否可交易标志
    istrader: bool = False  # 是否为交易者模式
    follow: bool = True  # 是否跟随主图
    plot_index: list[int] | None = None  # 绘图索引

    def set_default_stop_lines(self, id: BtID):
        """
        ## 设置默认的止损止盈线

        ### 参数：
        - id: BtID - 指标ID对象，用于唯一标识指标实例

        ### 功能：
        - 1. 创建一个2列的IndFrame对象，分别对应止损价和止盈价
        - 2. 设置指标属性：可绘图、与主图叠加、包含两条线
        - 3. 设置绘图样式为SpanList（用于显示价格区间）

        ### 注意：
        - 此方法通常在策略初始化时调用
        - 创建的stop_lines会在图表上显示为两条水平线/区间
        """
        from .core import IndFrame  # 延迟导入避免循环依赖
        # 创建止损止盈线指标框架
        self.stop_lines = IndFrame(
            (len(self.current_close), 2),  # 数据形状：长度与收盘价相同，2列
            id=id.copy(),  # 使用传入的ID副本
            isplot=True,  # 允许绘图显示
            overlap=True,  # 与主图叠加显示
            lines=['stop_price', 'target_price'],  # 两条线：止损价和目标价
            sname="stop_lines",  # 简称
            ind_name="stop_lines",  # 指标名称
            iscustom=True  # 必须设置为True，否则stop_price.new赋值不会生效
        )
        # 设置绘图样式为区间样式
        self.stop_lines.plotinfo.spanstyle = SpanList()


class IndicatorsBase(Base):
    """
    ## 量化指标基类（所有指标/数据对象的父类）
    ###核心定位：
    - 统一封装指标与K线数据的共性能力，包括数据访问、运算支持、绘图配置、多周期处理等，
    - 为子类（如Line、IndSeries、IndFrame、KLine）提供标准化接口，实现指标与数据的无缝交互。

    ### 📘 **文档参考**:
        - https://www.minibt.cn/minibt_basic/1.9minibt_indicatorsbase_class_guide/

    ### 核心设计：
    - 1. 多类型兼容：支持单列（Line）、一维（IndSeries）、多维（IndFrame、KLine）数据，统一接口操作
    - 2. 运算重载：内置算术/比较/逻辑运算符，支持指标间直接运算（如ma5 + ma10）
    - 3. 绘图配置：集成PlotInfo管理绘图属性（线型、颜色、是否主图叠加等）
    - 4. 多周期处理：支持数据上采样（resample）、跨周期关联，适配多周期策略
    - 5. 信号集成：内置交易信号字段（开多/平多/开空/平空），支持策略自动识别信号
    """
    # 内置指标数据类型
    __minibt_object__ = True
    # ------------------------------
    # 核心属性（控制数据类型与行为）
    # ------------------------------
    # 是否为多维数据（True=IndFrame，False=IndSeries/Line）
    # _isMDim: bool
    # 指标数据与原数据维度是否一致（True=自动被策略收集，False=仅内置使用）
    _dim_match: bool
    # 上采样数据中用于关联原数据的名称（多周期处理用）
    _upsample_name: str = ""
    # 交易信号标识列表（记录当前指标包含的信号类型）
    _issignal: list[str]
    # 交易信号字段（初始为None，策略运行中动态生成）
    _long_signal: IndSeries | None = None  # 开多信号
    _exitlong_signal: IndSeries | None = None  # 平多信号
    _short_signal: IndSeries | None = None  # 开空信号
    _exitshort_signal: IndSeries | None = None  # 平空信号
    # IndFrame数据中的Line字段列表（记录多维指标的列名）
    # _filed: list[str]
    # 线型配置（如实线、虚线，关联LineStyle类）
    # _lineinfo: LineStyle
    # 颜色配置（键为Line名，值为颜色值）
    # _color: dict
    # 指标核心设置（包含ID、维度、是否指标等元信息）
    _indsetting: IndSetting
    # 绘图配置（包含线型、颜色、是否显示等）
    _plotinfo: PlotInfo
    # 底层数值数组（快速访问用，与pandas_object同步）
    values: np.ndarray
    # 数据形状（行数×列数，如(1000,5)表示1000行5列）
    shape: tuple
    # 转换ID（上采样/下采样的标识）
    _resample_id: int
    # 转换索引（多周期数据对应的原数据索引）
    _resample_index: int
    # 转换后的数据（上采样/下采样后的完整数据）
    _resample_data: pd.DataFrame
    # 转换次数（记录数据被转换的次数）
    _resample_times: int
    # 因子数据框（存储指标衍生的因子数据）
    factors_df: pd.DataFrame
    # 合约信息（关联SymbolInfo，包含合约乘数、最小变动等）
    # _symbol_info: SymbolInfo
    # 停止线（如止损线、止盈线，关联IndFrame）
    _stop_lines: IndFrame
    # 指标内置数据集（管理原始数据、转换数据等）
    _dataset: DataFrameSet
    _tqobj: TqObjs
    price_line: bool
    price_label: bool
    _indframe:IndFrame | None=None
    _indseries:IndSeries | None=None
    _line:Line | None=None
    _kline:KLine | None=None
    _btindtype:BtIndType | None=None
    _klnetype:KLineType | None=None
    _indicatorclass:IndicatorClass | None=None
    __pandas_ta :PandasTa | None=None
    __talib:TaLib | None=None
    __tulip:TuLip | None=None
    __btind:BtInd | None=None
    __finta:FinTa | None=None
    __tqfunc:TqFunc | None=None
    __tqta:TqTa | None=None
    __pairtrading:Pair | None=None
    __factors:Factors | None=None
    __tradingsystem:TradingSystem | None=None

    def _pandas_ta(self)->PandasTa:
        if self.__pandas_ta is None:
            from .core import PandasTa
            self.__pandas_ta = PandasTa
        return self.__pandas_ta

    def _talib(self)->TaLib:
        if self.__talib is None:
            from .talib import TaLib
            self.__talib = TaLib
        return self.__talib

    def _tulip(self)->TuLip:
        if self.__tulip is None:
            from .tulip import TuLip
            self.__tulip = TuLip
        return self.__tulip
    
    def _finta(self)->FinTa:
        if self.__finta is None:
            from .finta import FinTa
            self.__finta = FinTa
        return self.__finta

    def _tqfunc(self)->TqFunc:
        if self.__tqfunc is None:
            from .tqfunc import TqFunc
            self.__tqfunc = TqFunc
        return self.__tqfunc
    
    def _tqta(self)->TqTa:
        if self.__tqta is None:
            from .tqta import TqTa
            self.__tqta = TqTa
        return self.__tqta

    def _btind(self)->BtInd:
        if self.__btind is None:
            from .btind import BtInd
            self.__btind = BtInd
        return self.__btind

    def _pairtrading(self)->Pair:
        if self.__pairtrading is None:
            from .pair import Pair
            self.__pairtrading = Pair
        return self.__pairtrading
    
    def _factors(self)->Factors:
        if self.__factors is None:
            from .factors import Factors
            self.__factors = Factors
        return self.__factors
    
    def _tradingsystem(self)->TradingSystem:
        if self.__tradingsystem is None:
            from ..tradingsymtem.tradingsystem import TradingSystem
            self.__tradingsystem = TradingSystem
        return self.__tradingsystem

    # 延迟导入 IndSeries 避免循环依赖
    def _get_indseries(self):
        if self._indseries is None:
            from .core import IndSeries
            self._indseries = IndSeries
        return self._indseries

    # 延迟导入 IndFrame 避免循环依赖
    def _get_indframe(self):
        if self._indframe is None:
            from .core import IndFrame
            self._indframe = IndFrame
        return self._indframe

    def _get_line(self):
        if self._line is None:  
            from .core import Line
            self._line = Line
        return self._line

    # 延迟导入 KLine 避免循环依赖
    def _get_kline_class(self):
        if self._kline is None:
            from .core import KLine
            self._kline = KLine
        return self._kline

    def _get_btindtype(self):
        if self._btindtype is None:
            from .core import BtIndType
            self._btindtype = BtIndType
        return self._btindtype

    def _get_klinetype(self):
        if self._klnetype is None:
            from .core import KLineType
            self._klnetype = KLineType
        return self._klnetype

    def _get_indicatorclass(self):
        if self._indicatorclass is None:
            from .core import IndicatorClass
            self._indicatorclass = IndicatorClass
        return self._indicatorclass

    # ------------------------------
    # 核心属性接口（关联KLine与策略）
    # ------------------------------

    @property
    def kline(self) -> KLine | None:
        """## 获取当前指标关联的KLine数据（属性接口）
        - 非指标对象（如KLine自身）返回自身，指标对象通过data_id关联到对应的KLine

        Returns:
            KLine: 关联的K线数据对象
        """
        """## 指标对应的KLine数据"""
        if not self.isindicator:
            return self
        return self._get_kline(self.data_id)
    
    @kline.setter
    def kline(self, value: KLine | None):
        if not self.isindicator:
            return
        if value is not None and type(value) == self._get_kline_class() and len(value) == len(self):
            self._dataset.kline_object = value

    @cachedmethod(attrgetter('cache'))
    def _get_kline(self, data_id: int) -> KLine:
        """## 缓存并获取KLine数据（内部方法，供kline属性调用）
        - 基于data_id从策略的_btklinedataset中获取对应的KLine，缓存结果避免重复查找
        - 无策略环境时沿 _dataset.source_object 链向上追溯，直到找到 KLine

        Args:
            kline_id (int): KLine的唯一标识ID

        Returns:
            KLine: 对应的K线数据对象
        """
        if not self.strategy_instances:
            _KLine = self._get_kline_class()
            if self._dataset.kline_object is not None:
                if type(self._dataset.kline_object) == _KLine:
                    return self._dataset.kline_object
            visited = {id(self)}
            node = self._dataset.source_object
            while node is not None and type(node) != _KLine:
                if id(node) in visited:
                    return None
                visited.add(id(node))
                # 安全遍历：避免遇到非 minibt 对象（如 pandas Series）时崩溃
                if not hasattr(node, '_dataset'):
                    break
                node = node._dataset.source_object
            self._dataset.kline_object = node if node is not None and type(node) == _KLine else None
            return self._dataset.kline_object
        return self.strategy_instance._btklinedataset[data_id]
    
    # ------------------------------
    # 交易逻辑与配置接口
    # ------------------------------
    def step(self):
        """## 交易逻辑扩展接口（抽象方法，可被子类重写）
        - 用于在指标内部嵌入交易逻辑（如基于指标值自动下单），策略运行时会自动调用

        ### 示例：
        >>> class The_Flash_Strategy(BtIndicator):
            params = dict(length=10, period=10, mult=3.,
                        pmax_length=12, pmax_mult=3., mom_rsi_val=60.)
            overlap = False
            def next(self):
                mom = self.close-self.close.shift(self.params.length)
                rsi_mom = mom.rsi(self.params.length)
                supertrend, direction = self.supertrend(
                    self.params.period, self.params.length).to_lines("trend", "dir")
                pmax_thrend = self.close.btind.pmax(
                    self.params.pmax_length, self.params.pmax_mult, mode="ema").thrend
                long_signal = pmax_thrend > 0.
                long_signal &= direction < 0.
                long_signal &= rsi_mom > self.params.mom_rsi_val
                exitlong_signal = self.close.cross_down(supertrend)
                short_signal = pmax_thrend < 0.
                short_signal &= direction > 0.
                short_signal &= rsi_mom > self.params.mom_rsi_val
                exitshort_signal = self.close.cross_up(supertrend)
                return long_signal, exitlong_signal, short_signal, exitshort_signal
            def step(self):
                if not self.kline.position.pos:
                    if self.long_signal.new:
                        self.kline.buy()
                    elif self.short_signal.new:
                        self.kline.sell()
                else:
                    if self.short_signal.new:
                        self.kline.set_target_size(-1)
                    elif self.long_signal.new:
                        self.kline.set_target_size(1)
        >>> class MyStrategy(Strategy):
                def __init__(self):
                    self.kline = self.kline_random(
                        data_length=3000, volatility=0.1)
                    self.tfs = The_Flash_Strategy(self.kline)
                def next(self):
                    self.tfs.step()
        """
        ...

    def get_first_valid_index(self, *args: tuple[np.ndarray, pd.Series], include_self: bool = False) -> int:
        """## 获取多个数组中第一个有效数据的起始索引

        - 计算多个数组或序列中第一个非NaN值的最大位置索引，
        - 用于确定技术指标计算的起始位置。

        Args:
            *args: 多个numpy数组或pandas Series

        Returns:
            int: 所有数组中第一个有效数据的最大起始索引
                即跳过所有开头的NaN值后的起始位置

        Example:
            >>> arr1 = np.array([np.nan, np.nan, 1, 2, 3])
            >>> arr2 = np.array([np.nan, 1, 2, 3, 4])
            >>> get_first_valid_index(arr1, arr2)
            2  # 因为arr1需要跳过2个NaN才能开始计算
        """
        args: list = [arg.values if hasattr(
            arg, "values") else arg for arg in args]
        if include_self and len(self.shape) == 1:
            args.insert(0, self.values)
        nan_counts = [len(arg[pd.isnull(arg)])
                      for arg in args if isinstance(arg, np.ndarray)]

        if len(nan_counts) == 0:
            return 0
        elif len(nan_counts) == 1:
            return nan_counts[0]
        else:
            return max(nan_counts)

    def get_indicator_kwargs(self, **kwargs) -> Addict:
        """## 整合指标配置参数（内部方法）
        合并_indsetting（指标设置）、_plotinfo（绘图配置）与用户传入的kwargs，返回统一参数字典

        Args:
            **kwargs: 用户传入的额外参数（优先级最高，覆盖默认配置）

        Returns:
            - Addict[str, Any]: 整合后的指标参数字典（Addict类型，支持属性式访问）
        """
        # 合并配置：指标设置 → 绘图配置 → 用户参数（优先级递增）
        result = Addict({**self._indsetting.vars, **self._plotinfo.vars})
        # 对于值为字典的执行字典更新
        if kwargs:
            key = []
            for k, v in kwargs.items():
                if isinstance(v, dict) and isinstance(result.get(k), dict):
                    result[k].update(v)
                    key.append(k)
            [kwargs.pop(k) for k in key]
        if kwargs:
            return Addict({**result, **kwargs})
        return result

    @staticmethod
    def get_max_missing_count(*args: tuple[pd.Series, np.ndarray]) -> int:
        """## 计算输入数据中缺失值数量的最大值（静态方法）
        仅处理np.ndarray或pd.Series类型，用于指标计算前的数据源质量校验

        Args:
            *args: 可变参数，每个参数为np.ndarray或pd.Series

        Returns:
            int: 所有输入数据中缺失值数量的最大值（无输入时返回0）
        """
        if not args:
            return 0
        # 计算每个输入的缺失值数量
        result = [len(arg[pd.isnull(arg)])
                  for arg in args if isinstance(arg, (pd.Series, np.ndarray))]
        if len(result) == 1:
            return result[0]
        return max(result) if result else 0

    # ------------------------------
    # 名称与ID配置接口（标识与关联）
    # ------------------------------
    @property
    def sname(self) -> str:
        """## 获取策略赋值的指标名称（属性接口）
        用于策略中标识指标（如self.ma5的sname为"ma5"），关联_plotinfo的sname字段

        Returns:
            str: 指标的策略内名称
        """
        return self._plotinfo.sname

    @sname.setter
    def sname(self, value) -> None:
        """## 设置策略赋值的指标名称（属性接口）
        仅接受非空字符串，用于自定义指标在策略中的标识

        Args:
            value (str): 新的指标名称
        """
        if value and isinstance(value, str):
            self._plotinfo.sname = value

    @property
    def ind_name(self) -> str:
        """## 获取指标自身名称（属性接口）
        用于标识指标类型（如"MA"、"RSI"），关联_plotinfo的ind_name字段

        Returns:
            str: 指标的类型名称
        """
        return self._plotinfo.ind_name

    @ind_name.setter
    def ind_name(self, value) -> None:
        """## 设置指标自身名称（属性接口）
        仅接受非空字符串，用于自定义指标的类型标识

        Args:
            value (str): 新的指标类型名称
        """
        if value and isinstance(value, str):
            self._plotinfo.ind_name = value

    @property
    def id(self) -> BtID:
        """## 获取指标的唯一标识（属性接口）
        BtID包含策略ID、数据ID、绘图ID等，用于多策略、多数据的关联

        Returns:
            BtID: 指标的唯一标识对象
        """
        """## BtID"""
        return self._indsetting.id

    @id.setter
    def id(self, value) -> None:
        """## 设置指标的唯一标识（属性接口）
        仅接受BtID类型，用于关联指标到特定策略与数据

        Args:
            value (BtID): 新的BtID对象
        """
        if value and isinstance(value, BtID):
            self._indsetting.id = value

    # ------------------------------
    # BtID分解接口（快速获取ID组件）
    # ------------------------------
    @property
    def strategy_id(self) -> int:
        """## 获取策略ID（属性接口，BtID分解）
        从_indsetting.id中提取strategy_id，用于关联到特定策略实例

        Returns:
            int: 策略的唯一标识ID
        """
        """## 策略ID"""
        return self._indsetting.id.strategy_id

    @property
    def sid(self) -> int:
        """## 获取策略ID（属性接口，与strategy_id功能一致，兼容简写）

        Returns:
            int: 策略的唯一标识ID
        """
        """## 策略ID"""
        return self._indsetting.id.strategy_id

    @property
    def plot_id(self) -> int:
        """## 获取绘图ID（属性接口，BtID分解）
        用于标识指标在绘图中的分组（如多合约绘图时区分不同子图）

        Returns:
            int: 绘图分组ID
        """
        """## 画图ID"""
        return self._indsetting.id.plot_id

    @property
    def data_id(self) -> int:
        """## 获取数据ID（属性接口，BtID分解）
        关联到对应的KLine数据，用于指标与K线数据的绑定

        Returns:
            int: 关联的KLineID
        """
        """## 数据ID"""
        return self._indsetting.id.data_id

    @property
    def resample_id(self) -> int:
        """## 获取转换ID（属性接口，BtID分解）
        用于标识上采样/下采样后的数据，支持多周期数据关联

        Returns:
            int: 数据转换ID
        """
        """## 转换ID"""
        return self._indsetting.id.resample_id

    @property
    def replay_id(self) -> int:
        """## 获取播放ID（属性接口，BtID分解）
        用于回放数据的标识，支持历史数据回放功能

        Returns:
            int: 数据回放ID
        """
        """## 播放ID"""
        return self._indsetting.id.replay_id

    # ------------------------------
    # 指标线配置接口（绘图与显示）
    # ------------------------------
    @property
    def lines(self) -> Lines:
        """## 获取指标线配置（属性接口）
        管理指标的所有线条（如MA5、MA10），支持字符串、列表、Lines对象赋值

        Returns:
            Lines: 指标线配置对象（包含所有线条名称与属性）
        """
        """## 指标线

        可设置value:
        >>> str,Iterable,Lines"""
        return self._plotinfo.lines

    @lines.setter
    def lines(self, value: Iterable | dict | str) -> None:
        """## 设置指标线配置（属性设置器）

        自动转换输入格式并更新指标线配置，支持三种输入类型：
        - 字典：键为旧指标线名称，值为新名称（仅更新字典中存在的旧指标线）
        - 字符串：单指标线名称（仅适用于IndSeries）
        - 可迭代对象：新指标线名称列表（需与原指标线数量一致，且元素均为非空字符串）

        处理逻辑：
        1. 校验输入格式并提取新旧指标线映射关系
        2. 更新内部指标线列表（new_lines）
        3. 若存在有效更新，同步更新绘图配置信息

        Args:
            value: 指标线配置值，支持dict、str或Iterable类型

        Print:
            输入格式不支持或不符合要求时触发
        """
        if not value:
            return  # 空值不处理

        # 初始化变量：存储新旧指标线映射、当前指标线列表
        new_key_mapping = {}
        current_lines = self.lines.values  # 当前指标线列表

        # 处理字典类型输入（{旧键: 新键}）
        if isinstance(value, dict):
            for old_key, new_key in value.items():
                if old_key in self.lines:  # 仅处理存在的旧指标线
                    new_key_mapping[old_key] = new_key
                    # 更新指标线列表中对应的旧键为新键
                    current_lines[current_lines.index(old_key)] = new_key

        # 处理字符串类型输入（仅单线条模式有效）
        elif isinstance(value, str) and self.H == 1 and current_lines[0] != value:
            # 记录原指标线与新名称的映射
            new_key_mapping[current_lines[0]] = value
            current_lines[0] = value

        # 处理可迭代对象（需与原指标线数量匹配）
        elif isinstance(value, Iterable) and len(self.lines) == len(value) and all([isinstance(v, str) and v for v in value]):
            value_list = list(value)
            # 生成新旧指标线映射（仅更新不同的部分）
            for idx, (old_key, new_key) in enumerate(zip(current_lines, value_list)):
                if old_key != new_key and old_key in self.lines:
                    new_key_mapping[old_key] = new_key
                    current_lines[idx] = new_key

        # 不支持的输入类型
        else:
            print(
                "不支持的输入格式！请使用：\n",
                "1. 字典：{旧指标线名称: 新指标线名称}\n",
                "2. 字符串：单指标线名称（IndSeries）\n",
                "3. 可迭代对象：与原指标线数量相同的字符串列表(IndFrame)",
            )

        # 若存在有效更新，同步更新绘图配置
        if new_key_mapping:
            self._plotinfo.lines = Lines(*current_lines)(self)
            self._plotinfo.rename_related_keys_using_mapping(new_key_mapping)

    @property
    def line(self) -> list[Line] | IndSeries:
        """## 获取多维指标的Line对象列表（属性接口）
        仅对多维数据（isMDim=True）有效，返回所有列对应的Line实例

        Returns:
            list[Line]: Line对象列表（非多维数据返回空列表）
        """
        if not self._indsetting.isMDim:
            return self
        # 从指标设置的line_filed中提取Line对象
        return [getattr(self, filed) for filed in self._plotinfo.line_filed]

    # ------------------------------
    # 绘图分类与样式接口
    # ------------------------------

    @property
    def category(self) -> CategoryString | str:
        """## 获取指标绘图分类（属性接口）
        标识指标的绘图类型（如"Candles"=蜡烛图、"MA"=均线、"RSI"=震荡指标）

        Returns:
            CategoryString | str: 绘图分类（支持CategoryString枚举或字符串）
        """
        return self._plotinfo.category

    @category.setter
    def category(self, value) -> None:
        """## 设置指标绘图分类（属性接口）
        自动注册新分类到Category枚举，支持自定义分类名称

        Args:
            value (str): 新的绘图分类名称（如"CustomIndicator"）
        """
        if value and isinstance(value, str):
            # 转换为CategoryString对象（兼容枚举）
            category = CategoryString(value)
            self._plotinfo.category = category
            # 若分类不在默认Category中，动态添加
            if category not in Category:
                setattr(Category, value.capitalize(), category)

    @property
    def iscandles(self) -> bool:
        """## 判断是否为蜡烛图类型（属性接口）
        基于category是否为"Candles"，用于绘图时区分K线与普通指标

        Returns:
            bool: True=蜡烛图，False=普通指标
        """
        """## 是否为蜡烛图"""
        return self._plotinfo.category.iscandles

    @property
    def isplot(self) -> dict | bool:
        """## 获取指标的绘图开关（属性接口）
        控制指标是否显示在图表中，支持单值（bool）或多线条配置（dict）

        Returns:
            dict | bool:
                - bool: 单线条指标的绘图开关
                - dict: 多线条指标的绘图开关（键为线条名，值为bool）
        """
        """## 是否画图

        可设置value:
        >>> IndSeries:bool
            IndFrame:bool,list[bool],tuple[bool],dict[str,bool]"""
        return self._plotinfo.isplot

    @isplot.setter
    def isplot(self, value) -> None:
        """## 设置指标的绘图开关（属性接口）
        批量控制所有线条的显示状态，自动同步信号叠加配置

        Args:
            value (bool | list[bool] | tuple[bool] | dict[str, bool]):
                - bool: 所有线条统一开关
                - list/tuple: 按线条顺序设置开关（长度需与线条数一致）
                - dict: 按线条名设置开关（键为线条名，值为bool）
        """
        # 批量设置线条的绘图开关
        self._plotinfo.set_lines_plot(value)
        # 同步更新信号的叠加配置
        self._plotinfo._set_signal_overlap()

    @property
    def height(self) -> int:
        """## 获取指标绘图高度（属性接口）
        控制指标在图表中的显示高度（像素），默认最小值20

        Returns:
            int: 绘图高度（像素）
        """
        """## 画图高度"""
        return self._plotinfo.height

    @height.setter
    def height(self, value: int) -> None:
        """## 设置指标绘图高度（属性接口）
        仅接受大于19的整数，确保绘图区域可见

        Args:
            value (int | float): 新的绘图高度（自动转换为整数）
        """
        if isinstance(value, (int, float)) and value > 19:
            self._plotinfo.height = int(value)

    @property
    def overlap(self) -> dict | bool:
        """## 获取指标是否主图叠加（属性接口）
        控制指标是否显示在主图（K线图）上，支持单值或多线条配置

        Returns:
            dict | bool:
                - bool: 单线条指标的叠加开关
                - dict: 多线条指标的叠加开关（键为线条名，值为bool）
        """
        """## 是否为主图叠加

        可设置value:
        >>> IndSeries:bool
            IndFrame:bool,list[bool],tuple[bool],dict[str,bool]"""
        return self._plotinfo.overlap

    @overlap.setter
    def overlap(self, value) -> None:
        """## 设置指标是否主图叠加（属性接口）
        批量控制所有线条的主图叠加状态

        Args:
            value (bool | list[bool] | tuple[bool] | dict[str, bool]):
                - bool: 所有线条统一叠加开关
                - list/tuple: 按线条顺序设置叠加（长度需与线条数一致）
                - dict: 按线条名设置叠加（键为线条名，值为bool）
        """
        self._plotinfo.set_lines_overlap(value)

    @property
    def plotinfo(self) -> PlotInfo:
        """## 获取完整的绘图配置（属性接口）
        返回PlotInfo对象，包含所有绘图相关配置，**不可直接赋值**

        Returns:
            PlotInfo: 绘图配置对象
        """
        """## 画图信息,不可赋值"""
        return self._plotinfo

    @property
    def signallines(self) -> list[str]:
        """## 获取信号线条名称列表（属性接口）
        从plotinfo中提取所有标记为信号的线条名称，用于策略识别交易信号

        Returns:
            list[str]: 信号线条名称列表
        """
        return self._plotinfo.signallines

    @property
    def issignal(self) -> bool:
        """## 判断指标是否包含交易信号（属性接口）
        基于signallines是否非空，用于策略筛选信号指标

        Returns:
            bool: True=包含信号，False=不包含信号
        """
        return bool(self._plotinfo.signallines)

    @property
    def candle_style(self) -> CandleStyle | None:
        """## 获取蜡烛图样式（属性接口）
        仅对非指标对象（如KLine）有效，包含蜡烛图颜色、线型等配置

        Returns:
            CandleStyle | None: 蜡烛图样式对象（非蜡烛图返回None）
        """
        return self._plotinfo.candlestyle

    @candle_style.setter
    def candle_style(self, value):
        """## 设置蜡烛图样式（属性接口）
        仅对非指标对象有效，自动将category设为"Candles"

        Args:
            value (CandleStyle): 蜡烛图样式对象
        """
        if not self.isindicator and isinstance(value, CandleStyle):
            self._plotinfo.candlestyle = value
            self._plotinfo.category = Category.Candles

    # ------------------------------
    # 多周期与数据类型接口
    # ------------------------------
    @property
    def ismain(self) -> bool:
        """## 判断跨周期指标是否在主周期显示（属性接口）
        用于多周期策略中，控制子周期指标是否在主周期图表中显示

        Returns:
            bool: True=主周期显示，False=子周期单独显示
        """
        """## 跨周期指标在主周期中显示"""
        return self._indsetting.ismain

    @ismain.setter
    def ismain(self, value) -> None:
        """## 设置跨周期指标是否在主周期显示（属性接口）
        自动同步plot_id与策略属性，确保主周期显示时数据关联正确

        Args:
            value (bool): 主周期显示开关
        """
        value = bool(value)
        self._indsetting.ismain = value
        if value:
            # 回放数据：plot_id同步为replay_id
            if self.isreplay:
                self.id.plot_id = self.id.replay_id
            # 转换数据：将指标注册为策略属性
            elif self.isresample:
                setattr(self.strategy_instance, self.sname, self())

    @property
    def isreplay(self) -> bool:
        """## 判断是否为回放数据（属性接口）
        用于历史数据回放功能，标识数据是否为回放模式

        Returns:
            bool: True=回放数据，False=实时/历史数据
        """
        """## 是否为播放数据"""
        return self._indsetting.isreplay

    @isreplay.setter
    def isreplay(self, value) -> None:
        """## 设置是否为回放数据（属性接口）
        仅接受布尔值，用于切换数据的回放状态

        Args:
            value (bool): 回放数据开关
        """
        self._indsetting.isreplay = bool(value)

    @property
    def isresample(self) -> bool:
        """## 判断是否为转换数据（属性接口）
        标识数据是否经过上采样/下采样（如1分钟→5分钟）

        Returns:
            bool: True=转换数据，False=原始数据
        """
        """## 是否为转换数据"""
        return self._indsetting.isresample

    @isresample.setter
    def isresample(self, value) -> None:
        """## 设置是否为转换数据（属性接口）
        仅接受布尔值，用于标记数据的转换状态

        Args:
            value (bool): 转换数据开关
        """
        self._indsetting.isresample = bool(value)

    @property
    def isMDim(self) -> bool:
        """## 判断是否为多维数据（属性接口）

        Is Multi Dimension 简写

        区分数据类型：True=IndFrame（多列），False=IndSeries/Line（单列）

        Returns:
            bool: True=多维数据，False=一维数据
        """
        """## 是否为多维数据"""
        return self._indsetting.isMDim

    # ------------------------------
    # 水平线配置接口（绘图辅助）
    # ------------------------------
    @property
    def span_style(self) -> SpanList:
        """## 获取水平线样式列表（属性接口）
        管理指标的水平线配置（如RSI的20/80分界线），支持批量添加

        Returns:
            SpanList: 水平线样式列表对象
        """
        """## 水平线"""
        return self._plotinfo.spanstyle

    @span_style.setter
    def span_style(self, value) -> None:
        """## 添加水平线样式（属性接口）
        批量添加水平线配置，支持单个样式或样式列表

        Args:
            value (SpanStyle | list[SpanStyle]): 水平线样式（单个或列表）
        """
        self._plotinfo.spanstyle += value

    @property
    def span_location(self) -> list[float]:
        """## 获取水平线位置列表（属性接口）
        返回所有水平线的Y轴位置（如[20.0, 80.0]）

        Returns:
            list[float]: 水平线位置列表
        """
        return self._plotinfo.span_location

    @span_location.setter
    def span_location(self, value):
        """## 设置水平线位置（属性接口）
        批量添加水平线位置，自动同步到span_style

        Args:
            value (float | list[float]): 水平线位置（单个或列表）
        """
        self._plotinfo.spanstyle += value

    @property
    def span_color(self) -> list[str, Colors]:
        """## 获取水平线颜色列表（属性接口）
        返回所有水平线的颜色配置（如["red", "green"]）

        Returns:
            list[str | Colors]: 水平线颜色列表
        """
        return self._plotinfo.span_color

    @span_color.setter
    def span_color(self, value):
        """## 设置水平线颜色（属性接口）
        批量更新所有水平线的颜色，支持字符串或Colors枚举

        Args:
            value (str | Colors | list[str | Colors]): 颜色配置（单个或列表）
        """
        self._plotinfo.span_color = value

    @property
    def span_dash(self) -> list[str, LineDash]:
        """## 获取水平线线型列表（属性接口）
        返回所有水平线的线型配置（如["solid", "dashed"]）

        Returns:
            list[str | LineDash]: 水平线线型列表
        """
        return self._plotinfo.span_dash

    @span_dash.setter
    def span_dash(self, value):
        """## 设置水平线线型（属性接口）
        批量更新所有水平线的线型，支持字符串或LineDash枚举

        Args:
            value (str | LineDash | list[str | LineDash]): 线型配置（单个或列表）
        """
        self._plotinfo.span_dash = value

    @property
    def span_width(self) -> list[float]:
        """## 获取水平线宽度列表（属性接口）
        返回所有水平线的宽度配置（如[1.0, 2.0]）

        Returns:
            list[float]: 水平线宽度列表
        """
        return self._plotinfo.span_width

    @span_width.setter
    def span_width(self, value):
        """## 设置水平线宽度（属性接口）
        批量更新所有水平线的宽度，支持浮点数或列表

        Args:
            value (float | list[float]): 宽度配置（单个或列表）
        """
        self._plotinfo.span_width = value

    # ------------------------------
    # 数据类型标识接口
    # ------------------------------
    @property
    def isindicator(self) -> bool:
        """## 判断是否为指标对象（属性接口）
        区分指标（如MA、RSI）与原始数据（如KLine）

        Returns:
            bool: True=指标对象，False=KLine数据
        """
        """## 是否为指标,反之为KLine数据"""
        return self._indsetting.isindicator

    @property
    def iscustom(self) -> bool:
        """## 判断是否为自定义数据（属性接口）
        标识数据是否为用户自定义（非系统生成）

        Returns:
            bool: True=自定义数据，False=系统数据
        """
        """## 是否为自定义数据"""
        return self._indsetting.iscustom

    @iscustom.setter
    def iscustom(self, value) -> None:
        """## 设置是否为自定义数据（属性接口）
        仅接受布尔值，用于标记数据的来源

        Args:
            value (bool): 自定义数据开关
        """
        self._indsetting.iscustom = bool(value)

    # ------------------------------
    # 数据集与工具接口
    # ------------------------------
    @property
    def ind_setting(self) -> IndSetting:
        """## 获取指标完整设置（属性接口）
        返回_indsetting对象，包含指标的所有元信息（ID、维度、类型等）

        Returns:
            IndSetting: 指标设置对象
        """
        """## 返回指标设置"""
        return self._indsetting

    @property
    def dataset(self) -> DataFrameSet:
        """## 获取指标内置数据集（属性接口）
        管理原始数据、转换数据、上采样数据等，支持多版本数据存储

        Returns:
            DataFrameSet: 数据集对象
        """
        """## 数据集"""
        return self._dataset

    @property
    def pandas_object(self) -> pd.DataFrame | pd.Series | corefunc:
        """## 获取底层Pandas对象（属性接口）
        返回指标对应的原生Pandas对象（DataFrame/Series），用于高级数据操作

        Returns:
            pd.DataFrame | pd.Series | corefunc: 原生Pandas对象
        """
        """## pandas数据"""
        try:
            return self._dataset.pandas_object
        except:
            return pd.DataFrame(
                self.values, columns=self.columns) if self.isMDim else pd.Series(self.values)

    @property
    def copy_object(self) -> pd.DataFrame | pd.Series | corefunc:
        return self._dataset.copy_object

    @property
    def ta(self) -> CoreFunc:
        """## 便捷访问当前实例的核心指标计算接口
        - minibt框架的核心指标计算类，封装了基础金融/数据指标的计算逻辑。
        - 该类基于输入的原始数据（如时间序列数据），提供统一的核心指标计算接口，\n
        - 支持通过属性调用各类指标计算方法，返回结果为pandas对象（Series/DataFrame）\n
        - 或numpy数组（np.ndarray），便于后续数据分析或可视化。

        Returns:
        - CoreFunc: 核心指标计算方法的封装对象（同indicators属性返回值）
        - CoreFunc调用的指标函数返回的是pandas对象

        Examples:
            >>> ma = self.kline.close.ta.pta_sma(30)
            print(ma.tail(), type(ma))
            9995    4968.100000
            9996    4968.366667
            9997    4968.533333
            9998    4968.700000
            9999    4968.900000
            Name: SMA_30, dtype: float64 <class 'pandas.core.IndSeries.Series'>

        """
        from .core import CoreIndicators  # 延迟导入避免循环依赖
        return CoreIndicators(self.pandas_object).indicators

    @property
    def kline_object(self) -> pd.DataFrame | corefunc | None:
        """## 获取转换前的原始K线数据（属性接口）
        无论数据是否经过转换（如HA K线、线性回归K线），始终返回最原始的K线数据

        Returns:
            pd.DataFrame | corefunc | None: 原始K线数据（无则返回None）
        """
        return self._dataset.kline_object

    @property
    def source_object(self) -> KLine | IndFrame | IndSeries | Line:
        """## 获取生成指标的数据源（属性接口）
        返回上层的原始数据

        Returns:
            KLine | IndFrame | IndSeries | Line: 原始数据源
        """
        return self._dataset.source_object

    @property
    def conversion_object(self) -> pd.DataFrame | pd.Series | corefunc | None:
        """## 获取转换前的数据（属性接口）
        返回数据转换（如HA、线性回归）前的原始数据，用于对比分析

        Returns:
            pd.DataFrame | pd.Series | corefunc | None: 转换前数据（无则返回None）
        """
        """## 转换前的pandas数据"""
        return self._dataset.conversion_object

    @property
    def custom_object(self) -> pd.DataFrame | pd.Series | corefunc | None:
        """## 获取自定义数据（属性接口）
        返回用户自定义的数据（如手动计算的指标），用于扩展分析

        Returns:
            pd.DataFrame | pd.Series | corefunc | None: 自定义数据（无则返回None）
        """
        """## 自定义pandas数据"""
        return self._dataset.custom_object

    # ------------------------------
    # 数据维度与长度接口
    # ------------------------------
    @property
    def V(self) -> int:
        """## 获取数据行数（属性接口，简写）
        等同于_length，返回数据的时间步数量

        Returns:
            int: 数据行数（时间步数量）
        """
        """## 行"""
        return self.shape[0]

    @property
    def length(self) -> int:
        """## 获取数据长度（属性接口，与V功能一致）
        返回数据的时间步数量，用于循环迭代与切片

        Returns:
            int: 数据长度（时间步数量）
        """
        """## 数据长度"""
        return self.shape[0]

    @property
    def H(self) -> int:
        """## 获取数据列数（属性接口，简写）
        返回数据的特征维度数量（如MA5+MA10为2列）

        Returns:
            int: 数据列数（特征维度）
        """
        """##  列"""
        shape = self.shape
        return shape[1] if len(shape) > 1 else 1

    # ------------------------------
    # 第三方指标库接口（集成常用工具）
    # ------------------------------
    @property
    def pta(self) -> PandasTa:
        """## 获取PandasTA指标库接口（属性接口）
        - pandas_ta指标指引
        - 集成PandasTA的所有指标（如sma、rsi、macd），支持链式调用

        ### 📘 **API文档参考**:
        - https://www.minibt.cn/minibt_api_reference/pandasta/

        Returns:
            PandasTa: PandasTA指标包装对象

        ### 核心功能：
        - 封装 pandas_ta 库的各类技术指标，提供统一的调用接口
        - 通过 @tobtind 装饰器自动处理指标参数校验、计算逻辑调用和返回值转换，确保输出为框架兼容的 IndSeries 或 IndFrame
        - 支持多维度技术分析场景，覆盖蜡烛图形态、趋势跟踪、动量判断、波动率计算等量化交易核心需求
        - 内置指标分类体系，便于按业务场景快速定位和调用目标指标

        ### 指标分类与包含列表：
        - 该类支持的指标按功能划分为以下 9 大类，具体包含指标如下：

        **1. 蜡烛图分析（Candles）**
        - 功能：蜡烛图形态识别、特殊蜡烛图转换（如布林带K线、Z评分标准化蜡烛图）
        - 包含指标：cdl_pattern（蜡烛图形态识别）、cdl_z（Z评分标准化蜡烛图）、ha（Heikin-Ashi布林带K线）

        **2. 周期分析（Cycles）**
        - 功能：识别市场价格的周期性规律，辅助判断趋势转折节点
        - 包含指标：ebsw（周期检测指标）

        **3. 动量指标（Momentum）**
        - 功能：衡量价格变化的速度和力度，判断趋势强度与潜在反转
        - 包含指标：ao、apo、bias、bop、brar、cci、cfo、cg、cmo、coppock、cti、er、eri、fisher、
        - inertia、kdj、kst、macd、mom、pgo、ppo、psl、pvo、qqe、roc、rsi、rsx、rvgi、slope、smi、
        - squeeze、squeeze_pro、stc、stoch、stochrsi、td_seq、trix、tsi、uo、willr

        **4. 重叠指标（Overlap）**
        - 功能：通过价格平滑、均线拟合等方式，凸显价格趋势方向
        - 包含指标：alma、dema、ema、fwma、hilo、hl2、hlc3、hma、ichimoku、jma、kama、linreg、
        - mcgd、midpoint、midprice、ohlc4、pwma、rma、sinwma、sma、ssf、supertrend、swma、t3、
        - tema、trima、vidya、vwap、vwma、wcp、wma、zlma

        **5. 收益指标（Performance）**
        - 功能：计算资产的收益情况，量化投资回报表现
        - 包含指标：log_return（对数收益）、percent_return（百分比收益）

        **6. 统计指标（Statistics）**
        - 功能：基于统计方法分析价格分布特征、离散程度等
        - 包含指标：entropy（熵值）、kurtosis（峰度）、mad（平均绝对偏差）、median（中位数）、
        - quantile（分位数）、skew（偏度）、stdev（标准差）、tos_stdevall（全维度标准差）、
        - variance（方差）、zscore（Z评分）

        **7. 趋势指标（Trend）**
        - 功能：识别和确认价格趋势方向、强度及持续时间
        - 包含指标：adx、amat、aroon、chop、cksp、decay、decreasing（下跌趋势）、dpo、
        - increasing（上涨趋势）、long_run（长期趋势）、psar、qstick、short_run（短期趋势）、
        - tsignals（趋势信号）、ttm_trend、vhf、vortex、xsignals（扩展趋势信号）

        **8. 波动率指标（Volatility）**
        - 功能：衡量价格波动的剧烈程度，评估市场风险
        - 包含指标：aberration、accbands、atr（平均真实波幅）、bbands（布林带）、
        - donchian（唐奇安通道）、hwc、kc（肯特纳通道）、massi、natr（归一化平均真实波幅）、
        - pdist、rvi、thermo、true_range（真实波幅）、ui

        **9. 成交量指标（Volume）**
        - 功能：结合成交量数据分析资金流向，辅助判断价格走势的有效性
        - 包含指标：ad（积累/派发指标）、adosc（震荡指标）、aobv（绝对OBV）、cmf（资金流向指数）、
        - efi（资金效率指标）、eom（资金流动指数）、kvo（成交量震荡指标）、mfi（资金流量指标）、
        - nvi（负成交量指数）、obv（能量潮指标）、pvi（正成交量指数）、pvol（价格成交量指标）、
        - pvr（价格成交量比率）、pvt（价格成交量趋势）


        ### 使用说明：
        1. 初始化：
        - 传入框架支持的 IndSeries 或 IndFrame 数据对象（需包含指标计算所需的基础字段，如 open、high、low、close、volume 等）
        >>> data = IndFrame(...)  # 框架内置数据对象（含OHLCV等基础字段）
        >>> ta = PandasTa(data)

        2. 指标调用：
        - 直接调用对应指标方法，传入必要参数（默认参数已适配常见场景，可按需调整）
        >>> #示例1：识别十字星蜡烛图形态
        >>> #返回框架内置IndFrame，含十字星形态识别结果
        >>> doji_result = self.data.cdl_pattern(name="doji")
        >>> #示例2：计算Heikin-Ashi布林带K线
        >>> ha_candles = self.data.ha()  # 返回框架内置IndFrame，含HA蜡烛图的open、high、low、close字段
        >>> #示例3：计算14期RSI动量指标
        >>> rsi_14 = self.data.close.rsi(length=14)  # 返回框架内置IndSeries，含14期RSI值

        3. 返回值特性：
        - 所有方法返回框架内置的 IndSeries 或 IndFrame 类型，可直接用于后续策略逻辑（如信号生成、风险控制），无需额外类型转换


        ### 注意事项：
        - 部分指标需特定基础字段（如成交量指标需 volume 字段），调用前确保输入数据包含所需字段
        - 指标参数（如 length 周期）可通过方法参数调整，未指定时使用 pandas_ta 默认值
        - 可通过 @tobtind 装饰器的 kwargs 参数配置填充缺失值（fillna）、数据偏移（offset）等辅助功能
        """
        return self._pandas_ta()(self)

    @property
    def talib(self) -> TaLib:
        """## 获取TA-Lib指标库接口（属性接口）
        - 集成TA-Lib的所有指标，支持高性能计算
        - 将目标数据转换为minibt内置指标数据，提供TA-Lib库中技术指标的Python接口。
        - 此类封装了TA-Lib的技术指标函数，使其能够与minibt框架无缝集成。

        ### 📘 **文档参考**:
        - API参考：https://www.minibt.cn/minibt_api_reference/talib/

        Returns:
        - TaLib: TA-Lib指标包装对象

        ### 主要特性：
        - 支持TA-Lib的所有技术指标类别
        - 自动处理数据格式转换
        - 提供统一的参数接口
        - 返回minibt兼容的IndSeries或IndFrame格式

        ### 使用示例：
        ```python
        # 从数据源创建TaLib实例
        ta = TaLib(data)

        # 从策略调用指标
        self.kline.talib

        # 计算希尔伯特变换-主导周期
        ht_period = ta.HT_DCPERIOD()
        ht_period = self.kline.close.talib.HT_DCPERIOD()
        ht_period = self.kline.close.HT_DCPERIOD()

        # 计算移动平均线
        sma = ta.SMA(length=20)
        sma = self.kline.close.talib.SMA(length=20)
        sma = self.kline.close.SMA(length=20)

        # 计算相对强弱指数
        rsi = ta.RSI(length=14)
        rsi = self.kline.close.talib.RSI(length=14)
        rsi = self.kline.close.RSI(length=14)
        ```

        ### 参数：
            data: 输入数据，可以是pandas Series或DataFrame格式

        ### 属性：
            _df: 存储输入数据的内部属性

        ### 方法：
        所有TA-Lib技术指标方法，按功能分类：
        - 周期指标 (Cycle Indicator Functions)
        - 价格变换 (Price Transform)
        - 动量指标 (Momentum Indicators)
        - 波动率指标 (Volatility Indicators)
        - 成交量指标 (Volume Indicators)
        - 趋势指标 (Trend Indicators)
        - 统计函数 (Statistic Functions)
        - 数学变换 (Math Transform)
        - 数学运算符 (Math Operators)

        ### 注意：
        - 使用前需要确保已安装TA-Lib库
        - 输入数据应包含所需的OHLCV列
        - 返回值会自动转换为minibt的IndSeries或IndFrame格式
        - 所有指标方法都支持**kwargs参数传递额外设置

        ### 版本要求：
        - Python 3.7+
        - TA-Lib 0.4.0+
        - minibt 兼容版本
        """
        return self._talib()(self)

    @property
    def tulip(self) -> TuLip:
        """## 获取Tulip指标库接口（属性接口）
        - 集成Tulip Indicators的所有指标，支持小众但实用的指标

        Returns:
            TuLip: Tulip指标包装对象
        """
        return self._tulip()(self)

    @property
    def btind(self) -> BtInd:
        """## 获取内置指标接口（属性接口）
        - 集成框架自定义的内置指标（如自定义MA、止损线）

        Returns:
            BtInd: 内置指标包装对象
        """
        return self._btind()(self)

    @property
    def finta(self) -> FinTa:
        """## 获取FinTa指标库接口（属性接口）
        - 集成FinTa的所有指标，专注于金融技术分析

        Returns:
            FinTa: FinTa指标包装对象
        """
        return self._finta()(self)

    @property
    def tqfunc(self) -> TqFunc:
        """## 获取天勤函数接口（属性接口）

        - 集成TQSDK特有的指标与工具函数，适配期货实盘
        - 封装天勤量化(TqSdk)的序列计算函数库，为技术指标和策略开发提供基础数学运算能力。

        ### 📘 **文档参考**:
        - API参考：https://www.minibt.cn/minibt_api_reference/tqfunc/
        - 天勤文档：https://tqsdk-python.readthedocs.io/en/latest/reference/tqsdk.tafunc.html

        Returns:
            TqFunc: 天勤指标包装对象

        ### 核心功能：
        - 序列位移计算：提供时间序列的滞后、超前等位移操作
        - 统计量计算：包含均值、标准差、极值等统计函数
        - 逻辑判断：支持交叉信号、条件计数等逻辑运算
        - 移动平均：多种类型的移动平均计算方法
        - 时间处理：时间格式转换和时间戳处理工具

        ### 使用说明：
        1. 初始化：传入minibt框架兼容的Series或DataFrame数据对象
        >>> # 类调用
            data = IndFrame(...)  # 包含OHLCV等基础字段的minibt数据对象
            tqfunc = TqFunc(data)
            # 通过指标调用
            self.kline.close.tqfunc

        2. 函数调用：直接调用对应函数方法，支持参数自定义
        >>> prev_close = close.tqfunc.ref(length=1)        # 获取前一期收盘价
            ma_20 = close.tqfunc.ma(length=20)             # 20周期简单移动平均

        ### 技术特点：
        - 天勤兼容：基于天勤官方函数库，确保计算准确性
        - 序列优化：针对金融时间序列数据特殊优化
        - 向量计算：支持批量数据处理，计算效率高
        - 边界处理：自动处理数据边界和缺失值情况
        - 类型安全：确保输入输出数据格式一致性
        """
        return self._tqfunc()(self)

    @property
    def tqta(self) -> TqTa:
        """## 获取天勤指标接口（属性接口）
        - 集成TQSDK特有的指标与工具函数，适配期货实盘
        - 基于天勤量化(TqSdk)的技术指标库封装，提供专业的技术分析指标计算。
        - 支持移动平均、振荡指标、趋势指标、量价指标等各类技术分析工具。

        ### 📘 **文档参考**:
        - API参考：https://www.minibt.cn/minibt_api_reference/tqfunc/
        - 天勤文档：https://tqsdk-python.readthedocs.io/en/latest/reference/tqsdk.ta.html

        Returns:
            TqTa: 天勤指标包装对象

        ### 核心功能分类：
        - 趋势指标：MA, EMA, MACD, 布林带等
        - 振荡指标：RSI, KDJ, WR, CCI, BIAS等  
        - 量价指标：OBV, VWAP, 成交量比率等
        - 统计指标：标准差、相关系数、回归分析等
        - 形态识别：高低点、支撑阻力等

        ### 使用示例：
        >>> data = IndFrame  # 包含OHLCV数据的minibt数据对象
        >>> tqta = TqTa(data)   # data数据必须包含指标计算时用到的字段
        >>> self.kline.close.tqta
        >>> 
        >>> # 趋势指标
        >>> ma_20 = close.tqta.ma(20)     # 指定close列计算20周期简单移动平均
        >>> ema_12 = close.tqta.ema(12)   # 12周期指数移动平均
        >>> macd_diff, macd_dea, macd_hist = tqta.macd()  # MACD指标
        >>> 
        >>> # 振荡指标
        >>> rsi_14 = tqta.rsi(14)         # 14周期RSI
        >>> k, d, j = tqta.kdj()          # KDJ随机指标
        >>> 
        >>> # 量价指标
        >>> obv_line = tqta.obv()         # 能量潮指标

        ### 技术特点：
        - 专业准确：基于天勤官方指标算法，确保计算准确性
        - 性能优化：针对金融时间序列数据进行算法优化
        - 完整兼容：与minibt数据框架无缝集成
        - 边界处理：自动处理数据边界和缺失值
        - 多周期支持：支持不同时间周期的指标计算
        """
        return self._tqta()(self)

    @property
    def pairtrading(self) -> Pair:
        """## 获取配对交易指标接口（属性接口）
        - 集成配对交易相关指标（如协整检验、价差计算）

        Returns:
            Pair: 配对交易指标包装对象
        """
        return self._pairtrading()(self)

    @property
    def factors(self) -> Factors:
        """## 获取因子分析接口（属性接口）
        - 生成与管理因子数据（如动量因子、价值因子），用于多因子策略

        Returns:
            Factors: 因子分析包装对象
        """
        return self._factors()(self)
    
    @property
    def tradingsystem(self) -> TradingSystem:
        """## 获取交易系统接口（属性接口）
        - 包含完整的指标分类体系（按照 pandas_ta 分类结构）
        - 提供交易策略的指标实现，如趋势跟踪、均值回归、动量交易等

        Returns:
            TradingSystem: 交易系统包装对象
        """
        return self._tradingsystem()(self)

    @property
    def __tradingview(self) -> TradingView:
        if self.__class__._trading_view is None:
            from .tradingview import TradingView
            self.__class__._trading_view = TradingView
        return self._trading_view

    @property
    def tradingview(self) -> TradingView:
        """
        ## TradingView策略指标集合接口（属性接口）
        - 用于将 TradingView 平台上广受欢迎的交易策略和指标转换为框架内置的指标数据类型（IndSeries/IndFrame）
        - NOTE:此类指标针对蜡烛图类指标或KLine类数据

        ### 📘 **API文档参考**:
        - https://www.minibt.cn/minibt_api_reference/tradingview/

        ### 核心功能：
        - 封装 TradingView 社区的优质策略指标，提供统一的调用接口
        - 通过 BtIndicator 基类自动处理指标参数校验、计算逻辑调用和返回值转换，确保输出为框架兼容的 IndSeries 或 IndFrame
        - 支持多维度交易策略场景，覆盖趋势跟踪、均值回归、波动率分析、动量交易等量化交易核心需求
        - 内置策略分类体系，便于按交易风格和策略类型快速定位和调用目标指标

        ### 策略分类与包含列表：
        该类支持的策略指标按功能划分为以下 8 大类，具体包含指标如下：

        #### **1. 趋势跟踪策略（Trend Following）**
        - 功能：识别和跟踪市场趋势方向，在趋势启动时入场，趋势结束时出场
        - 包含策略：Powertrend_Volume_Range_Filter_Strategy、Nadaraya_Watson_Envelope_Strategy、Adaptive_Trend_Filter、
        - Multi_Step_Vegas_SuperTrend_strategy、RJ_Trend_Engine、AlphaTrend、SuperTrend、SuperTrend_STRATEGY、Optimized_Trend_Tracker

        #### **2. 均值回归策略（Mean Reversion）**
        - 功能：在价格偏离均值时入场，预期价格回归均值时出场
        - 包含策略：DCA_Strategy_with_Mean_Reversion_and_Bollinger_Band、Bollinger_RSI_Double_Strategy、CM_Williams_Vix_Fix_Finds_Market_Bottoms

        #### **3. 突破策略（Breakout）**
        - 功能：在价格突破关键支撑阻力位时入场，捕捉趋势启动机会
        - 包含策略：Turtles_strategy、Turtle_Trade_Channels_Indicator_TUTCI、G_Channels、Twin_Range_Filter

        #### **4. 动量策略（Momentum）**
        - 功能：基于价格和成交量的动量变化识别交易机会
        - 包含策略：The_Flash_Strategy、WaveTrend_Oscillator、TonyUX_EMA_Scalper、Volume_Flow_Indicator

        #### **5. 波动率策略（Volatility）**
        - 功能：基于市场波动率变化调整交易参数和风险管理
        - 包含策略：STD_Filtered、PMax_Explorer、PMax_Explorer_STRATEGY、Chandelier_Exit、Pivot_Point_Supertrend

        #### **6. 机器学习策略（Machine Learning）**
        - 功能：基于自适应算法和AI技术优化策略参数
        - 包含策略：Quantum_Edge_Pro_Adaptive_AI、LOWESS

        #### **7. 信号处理策略（Signal Processing）**
        - 功能：基于信号处理理论分析价格数据
        - 包含策略：The_Price_Radio、ADX_and_DI

        #### **8. 风险管理策略（Risk Management）**
        - 功能：专注于头寸管理和风险控制的策略工具
        - 包含策略：Chandelier_Exit、Turtles_strategy

        ### 使用说明：
        #### 1. 初始化：
        - 传入框架支持的 KLine、IndFrame 或 IndSeries 数据对象（需包含策略计算所需的基础字段，如 open、high、low、close、volume 等）
        >>> data = IndFrame(...)  # 框架内置数据对象（含OHLCV等基础字段）
        >>> tv = TradingView(data)

        #### 2. 策略调用：
        - 直接调用对应策略方法，传入必要参数（默认参数已适配常见场景，可按需调整）
        >>> # 示例1：调用海龟交易策略
        >>> # 返回框架内置IndFrame，含多空信号和出场信号
        >>> turtle_signals = tv.Turtles_strategy(enter_fast=20, exit_fast=10, enter_slow=55, exit_slow=20)
        >>> # 示例2：调用超级趋势策略
        >>> supertrend_data = tv.SuperTrend_STRATEGY(Periods=10, Multiplier=3.0)
        >>> # 示例3：调用自适应AI策略
        >>> ai_scores = tv.Quantum_Edge_Pro_Adaptive_AI(LEARNING_PERIOD=40, ADAPTATION_SPEED=0.3)

        #### 3. 返回值特性：
        - 所有方法返回框架内置的 IndSeries 或 IndFrame 类型，可直接用于后续策略逻辑（如信号生成、风险控制），无需额外类型转换

        ### 策略集成示例：
        ```python
        class AdvancedStrategy(Strategy):
            def __init__(self):
                self.data = self.get_data(LocalDatas.test)
                self.tv = self.data.tradingview

                # 多重策略信号集成
                self.trend_signals = self.tv.SuperTrend_STRATEGY(Periods=10, Multiplier=3.0)
                self.momentum_signals = self.tv.WaveTrend_Oscillator(n1=10, n2=21, n3=9)
                self.volume_signals = self.tv.Volume_Flow_Indicator(length=130, coef=0.2)

            def next(self):
                if not self.data.position:
                    # 趋势确认 + 动量确认 + 成交量确认
                    long_condition = (self.trend_signals.long_signal.new & 
                                    (self.momentum_signals.wt1.new > 0) & 
                                    (self.volume_signals.vfi.new > 0))

                    short_condition = (self.trend_signals.short_signal.new & 
                                    (self.momentum_signals.wt1.new < 0) & 
                                    (self.volume_signals.vfi.new < 0))

                    # 执行交易逻辑
                    if long_condition:
                        self.data.buy()
                    elif short_condition:
                        self.data.sell()
        ```

        ### 注意事项：
        - **数据类型要求**：`self` 必须是 ``KLine`` 对象或包含 ``FILED.OHLCV``（open/high/low/close/volume）列的 ``IndFrame``，否则抛出 ``TypeError`` / ``ValueError``
        - 不同策略对基础数据字段要求不同，调用前确保输入数据包含所需字段（如成交量策略需要volume字段）
        - 策略参数对性能影响显著，建议通过回测优化确定最佳参数组合
        - 复杂策略（如AI自适应策略）需要足够的历史数据才能有效工作
        - 建议在模拟环境中充分测试策略表现后再实盘应用
        - 可结合框架的风险管理模块控制单策略和组合风险

        ### 性能优化建议：
        - 1. **参数调优**：使用框架的回测工具对策略参数进行优化
        - 2. **组合使用**：将不同策略信号组合使用，提高系统稳定性
        - 3. **风险分散**：在同一策略类别中选择多个不相关策略分散风险
        - 4. **市场适应**：根据不同市场环境动态调整策略权重
        - 5. **监控评估**：定期评估策略表现，及时调整或替换失效策略
        """
        # 数据类型校验：仅接受 KLine 或含 OHLCV 列的 IndFrame
        # KLine = self._get_kline_class()
        # if not isinstance(self, KLine):
        #     if not hasattr(self, 'pandas_object'):
        #         raise TypeError(
        #             f"❌：tradingview 指标仅支持 KLine 数据或包含 {FILED.OHLCV.tolist()} 列的 IndFrame，"
        #             f"当前传入类型为 {type(self).__name__}")
        #     if not all(col in self.pandas_object.columns for col in FILED.OHLCV):
        #         raise ValueError(
        #             f"❌：tradingview 指标需要包含 {FILED.OHLCV.tolist()} 列的 IndFrame 或 KLine 数据，"
        #             f"当前数据列: {self.pandas_object.columns.tolist()}")
        return self.__tradingview(self,ischeck=False)

    # ------------------------------
    # 策略与回测状态接口
    # ------------------------------

    @property
    def btindex(self) -> int:
        """## 获取当前回测索引（属性接口）
        - 同步策略的_btindex，标识当前处理到的K线位置

        Returns:
            int: 回测当前索引（从-1开始）
        """
        if not self._strategy_instances:
            return self.shape[0]-1
        return self.strategy_instance._btindex

    @property
    def islivetrading(self) -> bool:
        """## 判断是否为实盘交易（属性接口）
        - 同步策略的_is_live_trading状态，用于区分回测与实盘逻辑

        Returns:
            bool: True=实盘交易，False=回测
        """
        return self._is_live_trading

    @property
    def isline(self) -> bool:
        """## 判断指标是否为Line类型"""
        return (not self.isMDim) and self.__isline(self.sname)

    @cachedmethod(attrgetter('cache'))
    def __isline(self, sname):
        return type(self) == self._get_line()

    @property
    def new(self) -> float | int | bool | np.ndarray:
        """## 获取最新数据（属性接口）
        >>> indicator : [KLine, IndFrame, IndSeries, Line]
            等同于indicator.values[self.btindex]，快速访问当前K线的指标值
            等同于indicator.iloc[-1]

        Returns:
        >>> float | int | bool | np.ndarray : 最新数据（单值或数组）
        """
        return self.history()

    @new.setter
    def new(self, value: float | list[float]) -> None:
        """## 设置最新数据（属性接口）
        - 直接修改当前K线的指标值，支持单值（一维数据）或列表（多维数据）

        Args:
            value (float | list[float]): 新的最新数据值
        """
        # 只对自定义指标数据进行赋值，一般用于止损类
        if self.iscustom:
            if len(self.shape) == 1:
                # 一维数据：直接修改当前索引值
                self._mgr.blocks[0].values[self.btindex] = value
                # Line类型：同步更新源数据的对应列
                if self.isline:
                    index = self.source.lines.index(self.lines[0])
                    self.source._mgr.blocks[0].values[index][self.btindex] = value
                    self.source.pandas_object.iloc[self.btindex] = value
                # 清除缓存以确保下次访问返回最新值（pandas 3.0+ Copy-on-Write兼容）
                self.cache.clear()
            else:
                # 多维数据：按列更新当前索引值
                self._mgr.blocks[0].values[:, self.btindex] = value
                self.pandas_object.iloc[:, self.btindex] = value
                # 同步更新各Line字段
                for filed, v in zip(self._plotinfo.line_filed, value):
                    line = getattr(self, filed)
                    line._mgr.blocks[0].values[self.btindex] = v
                # 清除缓存以确保下次访问返回最新值（pandas 3.0+ Copy-on-Write兼容）
                self.cache.clear()

    @property
    def prev(self) -> float | int | bool | np.ndarray:
        """## 获取前1周期数据（属性接口）
        >>> indicator : [KLine, IndFrame, IndSeries, Line]
            等同于indicator.values[self.btindex-1]，快速访问「倒数第二根K线」的指标值（当前K线往前数第1个周期）
            等同于indicator.iloc[-2]

        Returns:
        >>> float | int | bool | np.ndarray : 前1周期数据（单值或数组）
        """
        return self.history(1)

    @property
    def sndprev(self) -> float | int | bool | np.ndarray:
        """## 获取前2周期数据（属性接口）
        >>> indicator : [KLine, IndFrame, IndSeries, Line]
            等同于indicator.values[self.btindex-2]，快速访问「倒数第三根K线」的指标值（当前K线往前数第2个周期）
            等同于indicator.iloc[-2]

        Returns:
            float | int | bool | np.ndarray : 前2周期数据（单值或数组）
        """
        return self.history(2)

    @property
    def trdprev(self) -> float | int | bool | np.ndarray:
        """## 获取前3周期数据（属性接口）
        >>> indicator : [KLine, IndFrame, IndSeries, Line]
            等同于indicator.values[self.btindex-3]，快速访问「倒数第四根K线」的指标值（当前K线往前数第3个周期）
            等同于indicator.iloc[-3]

        Returns:
        >>> float | int | bool | np.ndarray : 前3周期数据（单值或数组）
        """
        return self.history(3)

    @property
    def frthprev(self) -> float | int | bool | np.ndarray:
        """## 获取前4周期数据（属性接口）
        >>> indicator : [KLine, IndFrame, IndSeries, Line]
            等同于indicator.values[self.btindex-4]，快速访问「倒数第五根K线」的指标值（当前K线往前数第4个周期）
            等同于indicator.iloc[-4]

        Returns:
        >>> float | int | bool | np.ndarray : 前4周期数据（单值或数组）
        """
        return self.history(4)

    def history(self, lookback: int = 0, size: int = 1) -> float | int | bool | np.ndarray:
        """## 获取历史数据（方法接口）
        - 支持灵活的历史数据查询，可指定偏移量与数据长度，支持缓存优化

        Args:
            lookback (int, optional): 时间偏移量（0=最新，1=前一周期，依此类推）. Defaults to 0.
            size (int, optional): 数据长度（1=单值，>1=数组）. Defaults to 1.

        Returns:
        - float | int | bool | np.ndarray :
        - 单值：size=1时返回
        - 数组：size>1时返回（形状为(size,)）

        ### 示例说明：
        >>> self.ma5.history(0)        # 最新MA5值（等同于self.ma5.new）
            self.ma5.iloc[-1]
            self.ma5.history(1)      # 前一周期MA5值（等同于self.ma5.prev）
            self.ma5.iloc[-2]
            self.ma5.history(9)      # 10周期前的MA5值
            self.ma5.iloc[-10]
            self.ma5.history(0, 2)   # 最近10周期MA5值（含最新）
            self.ma5.iloc[-2:]
            self.ma5.history(2, 2)   # 10-20周期前的MA5值（不含最新）
            self.ma5.iloc[-4:-2])
        """
        return self.__history(lookback, size, self.btindex)

    @cachedmethod(attrgetter('cache'))
    def __history(self, lookback=0, size=1, btindex=0):
        """## 缓存历史数据查询结果（内部方法，供history调用）
        - 计算实际索引位置，处理非维度匹配数据的边界，避免越界访问

        Args:
            lookback (int): 时间偏移量
            size (int): 数据长度
            btindex (int): 当前回测索引

        Returns:
            float | np.ndarray: 历史数据（单值或数组）
        """
        # 计算实际数据索引（当前索引 - 偏移量）
        index = btindex - lookback
        # 非维度匹配数据：限制索引不超过最后有效索引
        if not self._indsetting.dim_match:
            index = min(self.shape[0]-1, index)
        # 多数据点：返回切片（size个数据）
        if size > 1:
            return self.values[index + 1 - size:index + 1]
        # 单数据点：返回指定索引值
        return self.values[index]

    def _update_replace(self, data: pd.DataFrame | pd.Series | None = None) -> None:
        """## 更新替换数据
        """
        if data is None:
            data = self._dataset.copy_object.iloc[:self.btindex+1]
        self._dataset.pandas_object = data
        self.__dict__["_mgr"] = data.__dict__["_mgr"]
        if self.isMDim:
            for l in self._plotinfo.lines:
                line_attr = f"_{l}"
                if not hasattr(self, line_attr) or l not in data.columns:
                    continue
                line: Line = getattr(self, line_attr)
                line_pandas_object = data[l]
                line._dataset.pandas_object = line_pandas_object
                line.__dict__["_mgr"] = line_pandas_object.__dict__["_mgr"]

    # ------------------------------
    # 多周期数据处理接口（上采样）
    # ------------------------------
    def upsample(self, **kwargs) -> IndFrame | IndSeries | Line:
        """
        ## 指标上采样与属性配置（方法接口）

        两种使用场景：
        - 跨周期转换：将低周期指标数据转换为高周期（如1分钟→5分钟），支持缓存复用
        - 属性配置：非转换数据时，通过 kwargs 覆盖指标的 PlotInfo/IndSetting/实例属性
          （如设置线型、颜色、是否跟随主图等）

        ### 参数覆盖优先级（非转换数据时）
        1. IndSetting 字段（ismain, isresample, isindicator 等）
        2. PlotInfo 字段（height, candlestyle, spanstyle, linestyle, overlap 等）
        3. KLineSetting 字段（follow, plot_index, isstop 等，仅 KLine）
        4. 实例属性/Property（candle_style, span_style, bull_color, bear_color 等）

        ### 参数

        - **reset** (bool): 是否重置上采样缓存（仅跨周期转换时有效）。
          True=重新计算，False=复用缓存。默认 False。
        - **kwargs**: 其余参数按上述优先级覆盖指标属性配置。
          常用示例：
          - `height=500`（PlotInfo）
          - `candle_style=CandleStyle(...)`（实例 property → 路由到 PlotInfo.candlestyle）
          - `span_style=[SpanStyle(location=50)]`（实例 property → 路由到 PlotInfo.spanstyle）
          - `follow=False`（KLineSetting）
          - `bull_color='#26a69a'`（实例 property → 路由到 PlotInfo.candlestyle.bull）

        ### 返回
        >>> IndFrame | IndSeries | Line: 上采样后的指标数据（转换数据），或 self（非转换数据）

        ### 示例
        >>> # 跨周期上采样：1分钟MA5 → 5分钟MA5
            self.ma5_5min = self.ma5.upsample(reset=True, length=20)

        >>> # 属性配置：设置指标高度和颜色
            self.rsi.upsample(height=400, span_style=[SpanStyle(location=30), SpanStyle(location=70)])
        """
        # 转换数据：优先使用缓存（reset=False时）
        if self.isresample:
            if not kwargs.pop("reset", False):
                if self._dataset.upsample_object is not None:
                    return self._dataset.upsample_object
            # 多策略指标转换：获取转换后的数据
            data = self.strategy_instances[self.sid]._multi_indicator_resample(
                self)
            if data is None:
                return self
            # 合并参数：指标设置 → 用户参数
            kwargs = {**self.ind_setting.copy_values, **kwargs}
            # 配置ID：同步转换ID
            kwargs.update(
                dict(
                    ismain=True,
                    _is_mir=True,
                    id=BtID(**self.id.filt_values(plot_id=self.resample_id))
                )
            )
            # 生成上采样数据对象（多维→IndFrame，一维→IndSeries）
            #from ._core import IndSeries, IndFrame  # 延迟导入避免循环依赖
            if data.shape[1] != 1:
                data = self._get_indframe()(data, ** kwargs)
            else:
                data = self._get_indseries()(data[:, 0], **kwargs)
            # 缓存上采样数据
            self._dataset.upsample_object = data
            data._upsample_name = "upsample"
            return data

        # 非转换数据：用户参数覆盖默认属性
        # 覆盖顺序：IndSetting > PlotInfo > KLineSetting > 实例属性(含property)
        if kwargs:
            for k, v in kwargs.items():
                if k in IndSetting:
                    try:
                        setattr(self._indsetting, k, v)
                    except:
                        ...
                elif k in PlotInfo:
                    try:
                        setattr(self._plotinfo, k, v)
                    except:
                        ...
                elif hasattr(self, '_klinesetting') and hasattr(type(self._klinesetting), k):
                    # KLine 独有属性：follow, plot_index, isstop, tradable 等
                    try:
                        setattr(self._klinesetting, k, v)
                    except:
                        ...
                else:
                    # 兜底：通过实例 property/setter 路由到正确存储
                    # 覆盖 candle_style, span_style, bull_color, bear_color, price_line 等
                    try:
                        setattr(self, k, v)
                    except:
                        ...
        return self

    def __call__(self, **kwargs) -> KLine | IndFrame | IndSeries | Line:
        """
        ## 调用运算符重载（方法接口，功能与 upsample 完全一致）

        支持两种场景：
        - 跨周期转换：`self.ma5_5min = self.ma5(length=20)` 等价于 `self.ma5.upsample(length=20)`
        - 属性配置：`self.data(height=500, follow=False)` 等价于 `self.data.upsample(height=500, follow=False)`

        ### 参数

        - **kwargs**: 与 `upsample()` 完全相同，按优先级路由到 IndSetting / PlotInfo / KLineSetting / 实例属性。
          详见 `upsample()` 的文档。

        ### 返回
        >>> IndFrame | IndSeries | Line | KLine: 上采样后的指标数据，或 self（属性配置时）

        ### 示例
        >>> # 上采样调用
            self.ma5_5min = self.ma5(length=20)

        >>> # 属性设置调用（通过 btplot 的 **kwargs 传递）
            self.data.btplot(candle_style={'bull_color': '#26a69a'})
            # 内部会调用 self.data(candle_style=...) → self.data.upsample(candle_style=...)
        """
        return self.upsample(**kwargs)

    # ------------------------------
    # 多指标并行计算接口
    # ------------------------------
    def multi_apply(self, *args: list[Callable, dict, IndFrame] | Multiply, **kwargs) -> tuple[IndFrame, IndSeries]:
        """
        ## 多指标并行计算（方法接口）
        - 支持批量计算多个指标，利用多线程提升效率，支持复杂指标组合

        Args:
            *args: 指标计算参数，每个参数为以下类型之一：
                - Multiply: 复杂指标组合（如Multiply(Ebsw, data=self.data)）
                - list: 简单指标配置（如[self.data.sma, dict(length=20)]）
            **kwargs: 并行计算参数：
                - max_workers: 最大线程数. Defaults to None.
                - thread_name_prefix: 线程名称前缀. Defaults to "".
                - initializer: 线程初始化函数. Defaults to None.
                - initargs: 初始化函数参数. Defaults to ().

        Returns:
        >>> tuple[IndFrame ,IndSeries]: 计算后的指标列表（按输入顺序）

        ### 示例：
        >>> # 批量计算多个指标
            self.ebsw, self.ma1, self.ma2 = self.multi_apply(
                Multiply(Ebsw, data=self.data),  # 复杂指标
                [self.data.sma, dict(length=20)], # 简单指标1
                [self.data.sma, dict(length=30)]  # 简单指标2
            )
        """
        kwargs.update(dict(data=self))
        return TPE.multi_run(*args, **kwargs)

    # ------------------------------
    # 数据迭代接口
    # ------------------------------
    def enumerate(self, *args: tuple[np.ndarray], start: int | None = None, offset: int = 1) -> zip[tuple]:
        """
        ## 带索引的多数组迭代（方法接口）
        - 同步迭代多个可迭代对象，返回索引与对应值，支持偏移量控制

        Args:
            *args: 可迭代对象（需长度一致）.
            start (int | None): 起始索引（None=从0开始）. Defaults to None.
            offset (int): 索引偏移量（如offset=1表示索引从1开始）. Defaults to 1.

        Returns:
        >>> zip[tuple[Any, ...]]: 迭代器，每个元素为(index, val1, val2, ...)

        Raises:
            AssertionError: 输入非可迭代对象或长度不一致时触发

        ### 示例：
        >>> # 迭代close和volume数组，索引从1开始
            for idx, close, vol in self.enumerate(self.close.values, self.volume.values, offset=1):
                print(f"索引{idx}: 收盘价{close}, 成交量{vol}")
        """
        """index,values...
        offset :int 偏移量"""
        # 校验输入：必须为可迭代对象
        assert all([isinstance(arg, Iterable) for arg in args]), "参数必须为可迭代对象"
        # 校验长度：所有输入必须长度一致
        assert len(set([len(arg) for arg in args])) == 1, "参数长度必须一致"
        # 确定迭代长度：start或数组长度
        length = start if isinstance(
            start, int) and start >= 0 else self.get_first_valid_index(*args)
        # 构建迭代器：索引 + 所有输入数组
        result = [range(length + offset, len(args[0])),]
        result.extend(list(args))
        return zip(*result)

    def get_lennan(self, *args: tuple[pd.Series, np.ndarray]) -> int:
        """### 获取参数数组中最大NAN的长度"""
        return get_lennan(*args)

    # ------------------------------
    # 策略实例关联接口
    # ------------------------------
    @property
    def strategy_instances(self) -> StrategyInstances:
        """
        ## 获取所有策略实例（属性接口）
        - 返回StrategyInstances对象，管理当前进程中的所有策略实例

        Returns:
        >>> StrategyInstances: 策略实例集合
        """
        """## 所有策略实例"""
        return self._strategy_instances

    @property
    def strategy_instance(self) -> Strategy | None:
        """
        ## 获取当前关联的策略实例（属性接口）
        - 基于strategy_id从strategy_instances中获取对应的策略实例

        Returns:
        >>> Strategy: 关联的策略实例
        """
        """## 策略实例"""
        if not self._strategy_instances:
            return
        return self._strategy_instances[self.id.strategy_id]

    # ------------------------------
    # 底层数据操作接口
    # ------------------------------
    def _apply_operate_string(self, string: str) -> IndFrame | IndSeries:
        """
        ## 通过字符串执行数据操作（内部方法，供运算符重载用）
        - 动态执行字符串中的代码，修改底层数据，返回新的指标对象

        Args:
            string (str): 执行的代码字符串（需定义'value'变量）

        Returns:
        >>> IndFrame  | IndSeries: 操作后的指标对象

        ### 示例：
        >>> # 执行a + b操作
            self._apply_operate_string('value = self.pandas_object + other')
        """
        # 执行代码字符串（定义value变量）
        exec(string)
        # 获取执行结果
        data = locals()['value']
        # 转换为指标对象并返回
        return self.__set_data(data.values)

    def _set_data_object(self, data, values: np.ndarray):
        """
        ## 直接设置数据对象的底层值（内部方法）
        - 修改Pandas对象的BlockManager，同步values数组，不触发数据校验

        Args:
            data: 目标数据对象（IndSeries/IndFrame）
            values (np.ndarray): 新的数值数组（需与数据形状匹配）
        """
        # 修改Block的values与位置信息
        data._mgr.blocks[0].values = values
        data._mgr.blocks[0]._mgr_locs = BlockPlacement(slice(len(values)))

    def _set_object(self, values: np.ndarray):
        """
        ## 直接设置当前对象的底层值（内部方法）
        - 修改自身的BlockManager，同步values数组，不触发数据校验

        Args:
            values (np.ndarray): 新的数值数组（需与数据形状匹配）
        """
        self._mgr.blocks[0].values = values
        self._mgr.blocks[0]._mgr_locs = BlockPlacement(slice(len(values)))

    def _inplace_pandas_object_values(self, data: pd.DataFrame | pd.Series) -> None:
        """
        ## 原地替换Pandas对象的底层数据（内部方法，供inplace_values调用）
        - 处理多Block场景（如DataFrame含不同类型字段），同步数据类型与形状

        Args:
            data (pd.DataFrame | pd.Series): 新的Pandas对象（需与原数据形状匹配）
        """
        # 更新数据集的Pandas对象
        self._dataset.pandas_object = data
        # 获取新数据的values数组
        values = data.values
        index = 0
        # 遍历所有Block，逐个替换值
        for block in self._mgr.blocks:
            existing_values = block.values
            shape = existing_values.shape
            target_dtype = existing_values.dtype

            # 提取当前Block对应的values（处理一维/二维）
            if len(values.shape) == 1:
                value = values
            else:
                if shape[0] == 1:
                    # 单行数据：按列提取
                    value = values[:, index]
                    value = value.reshape(shape)
                else:
                    # 多行数据：按列范围提取并转置
                    value = values[:, index:index + shape[0]].T

            # 处理datetime类型（确保类型匹配）
            if np.issubdtype(target_dtype, np.datetime64):
                value = value.astype(target_dtype)
            else:
                # 非datetime类型：转换为目标 dtype
                value = np.asarray(value, dtype=target_dtype)

            # 执行原地替换（优先处理有_ndarray属性的Block）
            if hasattr(existing_values, '_ndarray'):
                existing_values._ndarray[:] = value
            elif hasattr(existing_values, 'values'):
                np.copyto(existing_values.values, value)
            else:
                np.copyto(existing_values, value)

            # 更新Block索引（处理下一个Block）
            index += shape[0]

    def inplace_values(self, data: pd.DataFrame | pd.Series) -> None:
        """
        ## 原地替换底层数据（方法接口）
        - 直接修改当前指标的底层数据，不创建新对象，**谨慎使用**（无数据校验）

        Args:
            data (pd.DataFrame | pd.Series): 新的Pandas数据（需与原数据形状一致）

        ### 注意：
        - 仅支持Pandas对象输入
        - 形状不匹配时不执行操作
        - 可能导致数据一致性问题，建议仅在性能敏感场景使用
        """
        if type(data) not in PandasObject:
            return
        if self.shape != data.shape:
            return
        self._inplace_pandas_object_values(data)

    # ------------------------------
    # 运算符核心实现（内部方法）
    # ------------------------------
    def _apply_operator(self, other: IndFrame | IndSeries, op: str, reverse: bool = False, isbool: bool = False) -> IndFrame | IndSeries | np.ndarray:
        """
        ## 执行运算符操作（内部方法，供重载运算符调用）
        - 处理指标与指标/标量的运算，支持反向运算与布尔值转换

        Args:
            other (IndFrame  | IndSeries ): 运算对象（指标或标量）
            op (str): 运算符（如'+', '>', '&'）
            reverse (bool, optional): 是否反向运算（如a - b 反向为 b - a）. Defaults to False.
            isbool (bool, optional): 是否为布尔运算（自动转换为bool类型）. Defaults to False.

        Returns:
        >>> IndFrame  | IndSeries | np.ndarray : 运算结果（指标对象或原生类型）
        """
        # 处理运算对象：提取Pandas对象（若为指标）
        other = other.pandas_object if hasattr(
            other, 'pandas_object') else other

        # 布尔运算：自动转换为bool类型
        if isbool:
            if ispandasojb(other):
                # 双方均为Pandas对象：都转为bool
                str1, str2 = reverse and [
                    'other.astype(np.bool_)', 'self.pandas_object.astype(np.bool_)'] or [
                    'self.pandas_object.astype(np.bool_)', 'other.astype(np.bool_)']
            else:
                # 单方为Pandas对象：仅转换自身为bool
                str1, str2 = reverse and [
                    'other', 'self.pandas_object.astype(np.bool_)'] or [
                    'self.pandas_object.astype(np.bool_)', 'other']
        # 算术运算：保持原始类型
        else:
            str1, str2 = reverse and [
                'other', 'self.pandas_object'] or [
                'self.pandas_object', 'other']

        # 构建运算代码字符串并执行
        string = f'value=({str1} {op} {str2})'
        exec(string)
        data = locals()['value']
        # 转换为指标
        return self.__set_data(data.values)

    def __set_data(self, data: np.ndarray) -> IndFrame | IndSeries:
        """
        ## 将numpy数组转换为框架内置指标对象（内部方法，供运算/数据处理调用）
        - 根据输入数组维度自动选择返回IndSeries（一维）或IndFrame（多维），
        - 并继承当前指标的配置参数（如ID、绘图信息），确保数据一致性。

        Args:
            data (np.ndarray): 待转换的numpy数组（需与原数据长度匹配）

        Returns:
        >>> IndFrame  | IndSeries: 转换后的框架内置指标对象
        - IndSeries：输入为一维数组（shape=(N,)）时返回
        - IndFrame：输入为多维数组（shape=(N, M)）时返回

        ### 核心逻辑：
        - 1. 检测数组维度：通过shape判断是否为多维数据
        - 2. 继承配置参数：调用get_indicator_kwargs获取当前指标的配置（ID、绘图信息等）
        - 3. 生成指标对象：根据维度生成对应类型的内置指标，确保属性一致
        """
        if len(data.shape) > 1:
            # 多维数组（如N行M列）→ 转换为IndFrame对象
            return self._get_indframe()(data, **self.get_indicator_kwargs(isindicator=True))
        else:
            # 一维数组（如N行1列）→ 转换为IndSeries对象
            return self._get_indseries()(data, **self.get_indicator_kwargs(isindicator=True))

    # ------------------------------
    # Pandas方法适配装饰器
    # ------------------------------
    def _wrap_pandas_method_to_indicator(self, func: Callable) -> Callable:
        """
        ## 包装Pandas原生方法，将返回结果自动转换为框架内置指标类型（IndFrame/IndSeries）
        - 解决Pandas方法与框架指标对象的兼容性问题，确保方法调用后仍可链式使用框架功能。

        Args:
            func: 待包装的Pandas原生方法（如pd.Series.mean、pd.DataFrame.rolling等）

        Returns:
        >>> 包装后的方法，返回框架内置指标对象（保留索引和配置信息）
        """

        @wraps(func)
        def wrapper(*args, **kwargs):
            arguments = get_func_args_dict(func, *args, **kwargs)
            func_name = func.__name__
            if "kwargs" in kwargs and kwargs["kwargs"]:
                extra_kwargs = kwargs.pop("kwargs")
                kwargs = {**kwargs, **extra_kwargs}
            pandas_object = self.pandas_object
            pandas_func = getattr(pandas_object, func_name)
            pandas_kw_params = set(signature(pandas_func).parameters.keys())
            method_kwargs = {}
            frame_var_kwargs = {}
            if func_name in SPECIAL_FUNC:
                for k, v in kwargs.items():
                    if k in IndSetting or k in PlotInfo:
                        frame_var_kwargs.update({k: v})
                    else:
                        method_kwargs.update({k: v})
            else:
                for k, v in kwargs.items():
                    if k in pandas_kw_params:
                        method_kwargs.update({k: v})
                    else:
                        frame_var_kwargs.update({k: v})

            # --------------------- 调用 Pandas 原生方法 ---------------------
            try:
                # 有位置参数就传 *converted_var_positional，没有就只传 kwargs
                data = pandas_func(
                    *args, **method_kwargs)
            except Exception:
                # 异常时二次转换（逻辑与 try 块保持一致）
                # # 处理框架*args中的指标对象
                converted_var_positional = []
                for arg in args:
                    if type(arg) in self._get_btindtype():
                        converted_var_positional.append(arg.pandas_object)
                    else:
                        converted_var_positional.append(arg)

                # 处理关键字参数中的指标对象
                for k, v in method_kwargs.items():
                    if type(v) in self._get_btindtype():
                        method_kwargs[k] = v.pandas_object

                # 异常处理块也用正确的参数传递逻辑
                data = pandas_func(
                    *converted_var_positional, **method_kwargs)

            # --------------------- 6. 处理返回值 ---------------------
            if data is None and ("inplace" in arguments or not arguments.get("copy", True)):
                data = pandas_object
            as_internal = arguments.get('as_internal', True)
            if not as_internal:
                return data
            if not options.check_conversion_mode(data, self):
                return data
            isMDim = len(data.shape) > 1
            inplace = arguments.get('inplace', None)
            copy = arguments.get('copy', True)
            indicator_kwargs = self.get_indicator_kwargs(**frame_var_kwargs)
            indicator_kwargs["lines"] = list(
                data.columns) if isMDim else [func_name,]
            if inplace or (isinstance(copy, bool) and not copy):
                if isMDim:  # and self.shape[1] != data.shape[1]:
                    data = self._get_indframe()(data.values, **indicator_kwargs)
                    self.__dict__.update(data.__dict__)
                else:
                    if len(self.shape) > 1:
                        return self._get_indseries()(data.values, **indicator_kwargs)
                    self[:] = data.values
                return self

            if isMDim:
                return self._get_indframe()(data.values, **indicator_kwargs)
            else:
                return self._get_indseries()(data.values, **indicator_kwargs)

        return wrapper

    # ------------------------------
    # 多条件批量数据更新
    # ------------------------------
    def ifs(self, *args, other: pd.Series | np.ndarray | Any = None, filed: str | None = "") -> IndSeries | None:
        """
        ## 多条件批量数据更新（方法接口）
        - 类似Excel的IFS函数，按顺序处理多组「条件-值」对，
        - 满足条件时替换目标序列的值，不满足则保持原值，最终返回处理后的指标对象。

        ### 核心场景：
        - 策略信号生成（如突破均线开多、跌破均线平仓）
        - 数据清洗（如异常值替换、区间划分）
        - 动态参数调整（如不同行情下使用不同周期的MA）

        Args:
            *args: 可变参数，需为偶数个，格式为(cond1, value1, cond2, value2, ...)
                cond (np.ndarray | pd.Series | IndSeries): 布尔条件（长度需与目标序列一致）
                value (Any): 满足cond时替换的值（支持标量、数组，需与序列元素类型兼容）
            other (pd.Series | np.ndarray | Any, optional): 待处理的目标序列. Defaults to None.
                - 未指定时：自动从当前指标提取（多维取指定字段，一维取自身）
                - 标量/可迭代对象：自动转换为与当前指标长度一致的Series
            filed (str | None, optional): 多维指标的目标字段名. Defaults to "".
                - other未指定且为多维指标时，用于指定待处理的字段（如"close"、"ma5"）
                - 为空或字段不存在时，默认使用第一个线条（self.lines[0]）

        Returns:
        >>> IndSeries | None: 处理后的框架内置IndSeries对象；参数不合法时返回None

        ### 关键逻辑：
        - 1. 参数校验：确保args为偶数个（成对的条件-值），避免逻辑错误
        - 2. 目标序列初始化：统一将other转换为Pandas Series，确保处理逻辑一致
        - 3. 条件迭代执行：按顺序应用每组条件，后执行的条件会覆盖先执行的结果（优先级更高）
        - 4. 结果转换：将处理后的Series转为框架内置IndSeries对象，保持指标属性一致性

        ### 示例：
        >>> # 1. 策略信号生成：MA5上穿MA10开多，MA5下穿MA10平多
            ma5_cross_up_ma10 = self.ma5.cross_up(self.ma10)  # 开多条件
            ma5_cross_down_ma10 = self.ma5.cross_down(self.ma10)  # 平多条件
            self.long_signal = self.ifs(
                ma5_cross_up_ma10, 1,    # 满足开多条件→设为1（开多信号）
                ma5_cross_down_ma10, 0,  # 满足平多条件→设为0（平多信号）
                other=0  # 初始值为0（无信号）
            )

        >>> # 2. 数据清洗：替换收盘价异常值（>3倍标准差设为均值，<0设为0）
            close = self.data.close
            cond1 = close > close.mean() + 3 * close.std()  # 上异常值
            cond2 = close < 0  # 负价格（无效）
            self.clean_close = self.ifs(
                cond1, close.mean(),  # 上异常值→替换为均值
                cond2, 0,             # 负价格→替换为0
                other=close           # 目标序列为原始收盘价
            )
        """
        # 参数校验：args必须为偶数个（成对的条件-值）
        if args and len(args) >= 2:
            if other is None and len(args) % 2 != 0:
                *args, other = args
            if len(args) % 2 != 0:
                args = list(args)[:len(args)-1]
            else:
                args = list(args)
            if other is not None:
                # 目标序列初始化：处理other的不同输入类型
                if isinstance(other, (bool, int, float)):
                    # 标量→转换为与当前指标长度一致的Series（填充标量值）
                    other = pd.Series(self.full(other))
                # 原代码笔误"len(other=self.V)"已修正
                elif isinstance(other, Iterable) and len(other) == self.V:
                    # 可迭代对象（长度匹配）→ 转换为Series
                    other = pd.Series(other)
                else:
                    # 未指定other→从当前指标提取目标序列
                    if self.H != 1:
                        # 多维指标：按filed提取字段，默认取第一个线条
                        if filed not in self.lines:
                            filed = self.lines[0]
                        other = getattr(self, filed)
                    else:
                        # 一维指标：直接取自身
                        other = self
                    # 转换为Pandas Series（统一处理格式）
                    other = other.pandas_object

            # 迭代执行多组条件-值对（后条件覆盖前条件）
            for i in range(0, len(args), 2):
                cond, value = args[i], args[i+1]
                # 核心逻辑：满足cond时用value替换，否则保持other原值
                # 注：原代码other.where(cond, value)逻辑反了，已修正为np.where
                other = np.where(cond, value, other)

            # 转换为框架内置IndSeries对象并返回
            return self._get_indseries()(other)
        # 参数不合法（如args为奇数个）→ 返回None

    # ------------------------------
    # 便捷数据生成接口
    # ------------------------------
    @property
    def ones(self) -> IndSeries:
        """
        ## 生成全1序列（属性接口）
        - 返回与当前指标长度一致的全1 IndSeries对象，用于权重计算、信号标记等场景。

        Returns:
        >>> IndSeries: 全1序列（长度=self.V）

        ### 示例：
        >>> # 等权重组合2个指标
            weight = self.ones * 0.5  # 权重=0.5
            combined_ind = self.ma5 * weight + self.ma10 * weight
        """
        return self._get_indseries()(np.ones(self.V))

    def full(self, value=np.nan) -> IndSeries:
        """
        ## 生成填充指定值的序列（方法接口）
        - 返回与当前指标长度一致、所有元素为指定值的IndSeries对象，
        - 用于初始化、缺失值填充、固定阈值标记等场景。

        Args:
            value (Any, optional): 填充值（支持标量、np.nan）. Defaults to np.nan.

        Returns:
        >>> IndSeries: 填充后的序列（长度=self.V）

        ### 示例：
        >>> # 初始化空信号序列（默认值np.nan）
            self.signal = self.full()
            # 生成固定止损阈值序列（值=1000）
            self.stop_loss = self.full(1000)
        """
        return self._get_indseries()(np.full(self.V, value))

    @property
    def zeros(self) -> IndSeries:
        """
        ## 生成全0序列（属性接口）
        - 返回与当前指标长度一致的全0 IndSeries对象，用于初始化、无信号标记等场景。

        Returns:
        >>> IndSeries: 全0序列（长度=self.V）

        ### 示例：
        >>> # 初始化收益序列（默认值0）
            self.daily_return = self.zeros
        """
        return self._get_indseries()(np.zeros(self.V))

    # ------------------------------
    # 哈希与比较方法（确保实例可哈希）
    # ------------------------------
    # def __eq__(self, other):
    #     """保留对象身份比较"""
    #     return self is other

    def __hash__(self):
        """### 基于对象身份生成哈希值"""
        return hash(id(self))

    # ------------------------------
    # 属性访问拦截（Pandas方法自动适配）
    # ------------------------------
    def __getattribute__(self, item) -> KLine | IndFrame | IndSeries | Line:
        """
        ## 重写属性访问方法（内部方法）
        - 拦截Pandas方法的访问请求，自动应用__pandas_object_method装饰器，
        - 实现"调用指标的Pandas方法→返回框架内置指标对象"的无缝衔接，无需手动转换。

        Args:
            item (str): 属性/方法名称（如"mean"、"rolling"、"sname"）

        Returns:
        >>> Any:
            - 若为Pandas方法：返回装饰后的方法（返回指标对象）
            - 若为普通属性/方法：返回原始结果（如sname、btindex）
        """
        # 拦截Pandas方法：自动装饰并返回
        if item in pandas_method:
            return self._wrap_pandas_method_to_indicator(super().__getattribute__(item))
        # 普通属性/方法：直接返回原始结果
        return super().__getattribute__(item)

    # ------------------------------
    # 滚动窗口计算接口
    # ------------------------------
    def rolling(
        self,
        window=None,
        min_periods=None,
        center=False,
        win_type=None,
        on=None,
        axis=0,
        closed=None,
        step=None,
        method="single",
    ) -> BtRolling:
        """## 滚动窗口计算（方法接口）
        - 与Pandas的rolling方法用法完全一致，但返回框架自定义的BtRolling对象，
        - 支持后续调用rolling_apply、mean、std等方法时直接返回内置指标对象，
        - 无需手动转换Pandas结果。

        ### 核心场景：
        - 技术指标计算（如滚动均线MA、滚动标准差Bollinger Band）
        - 风险指标计算（如滚动最大回撤、滚动夏普比率）
        - 特征工程（如滚动成交量均值、滚动价格波动）

        Args:
            window (int | str | pd.Timedelta, optional): 窗口大小. Defaults to None.
                - 整数：固定窗口长度（如5=5个时间步）
                - 字符串：时间窗口（如"5D"=5天，需数据含datetime索引）
                - pd.Timedelta：时间窗口（如pd.Timedelta(days=5)）
            min_periods (int, optional): 窗口内最小非缺失值数量. Defaults to None.
                - 小于该数量时结果为NaN，默认等于window（需窗口完全填充）
            center (bool, optional): 是否将窗口中心作为结果位置. Defaults to False.
                - True：结果对齐窗口中心，False：结果对齐窗口右侧（默认）
            win_type (str, optional): 窗口权重类型. Defaults to None.
                - 如"boxcar"（矩形窗，默认）、"hanning"（汉宁窗）、"blackman"（布莱克曼窗）
            on (str, optional): 用于滚动的列名（仅DataFrame有效）. Defaults to None.
            axis (int, optional): 滚动轴（0=行方向，1=列方向）. Defaults to 0.
            closed (str, optional): 窗口闭合方式（仅整数窗口有效）. Defaults to None.
                - "right"（右闭，默认）、"left"（左闭）、"both"（两端闭）、"neither"（两端开）
            step (int, optional): 窗口步长（每step个数据计算一次）. Defaults to None.
                - 默认1（每个数据都计算）
            method (str, optional): 计算方法（"single"=单线程，"table"=矢量化）. Defaults to "single".

        Returns:
        >>> BtRolling: 框架自定义的滚动窗口对象，支持链式调用后续计算方法

        ### 示例：
        >>> # 1. 计算5日滚动均线（MA5）
            self.ma5 = self.data.close.rolling(
                window=5).mean(overlap=True)  # 返回IndSeries对象

        >>> # 2. 计算20日滚动标准差（用于布林带）
            self.std20 = self.data.close.rolling(
                window=20).std()  # 返回IndSeries对象

        >>> # 3. 多列同时计算滚动均值（DataFrame）
            self.rolling_mean = self.data.loc[:, ["open", "high", "low", "close"]].rolling(
                window=10).mean()  # 返回IndFrame对象
        """
        """## 与pandas数据rolling方法使用一致,返回的是IndFrame或IndSeries数据"""
        return BtRolling(
            self,
            window=window,
            min_periods=min_periods,
            center=center,
            win_type=win_type,
            on=on,
            axis=axis,
            closed=closed,
            step=step,
            method=method
        )

    def rolling_groupby(
        self,
        by,
        window=None,
        min_periods=None,
        center=False,
        win_type=None,
        on=None,
        axis=0,
        closed=None,
        step=None,
        method="single",
        selection=None,
    ) -> BtRollingGroupby:
        """## 分组滚动窗口计算（方法接口）

        - 与 Pandas ``groupby(...).rolling()`` 用法对应，但返回框架自定义的 ``BtRollingGroupby`` 对象，
        - 每个分组内部独立维护滚动窗口，分组间互不干扰（窗口边界不跨越分组），
        - 支持后续调用 ``mean``、``std``、``sum``、``quantile``、``corr`` 等方法时直接返回内置指标对象，
        - 无需手动转换 Pandas 结果。

        ### 核心场景：
        - 不同行业 / 板块的分组滚动均线计算（按 ``sector`` 分组）
        - 多品种分组的历史波动率、滚动相关性统计
        - 分组内排名指标的滚动计算

        Args:
            by: 分组依据，遵循 pandas ``DataFrame.groupby(by)`` 的参数规范：
                - ``str``：单个列名（如 ``"sector"``）
                - ``list[str]``：多个列名（如 ``["sector", "exchange"]``）
                - ``pd.Series``：自定义分组序列
                - 其他 pandas groupby 支持的 ``by`` 参数形式
            window (int | str | pd.Timedelta, optional): 窗口大小. Defaults to None.
                - 整数：固定窗口长度（如20=20个时间步）
                - 字符串：时间窗口（如"5D"=5天，需数据含datetime索引）
                - pd.Timedelta：时间窗口（如pd.Timedelta(days=5)）
            min_periods (int, optional): 窗口内最小非缺失值数量. Defaults to None.
                - 每组内独立计算，不足则结果为 NaN
            center (bool, optional): 是否将窗口中心作为结果位置. Defaults to False.
            win_type (str, optional): 窗口权重类型. Defaults to None.
                - 如"boxcar"（矩形窗，默认）、"triang"（三角窗）、"hanning"（汉宁窗）
            on (str, optional): 用于滚动的列名（仅 DataFrame 有效）. Defaults to None.
            axis (int, optional): 滚动轴（0=行方向，1=列方向）. Defaults to 0.
                pandas 3.0+ 不再支持 axis 参数，框架内部自动兼容
            closed (str, optional): 窗口闭合方式（仅整数窗口有效）. Defaults to None.
                - "right"（右闭，默认）、"left"（左闭）、"both"（两端闭）、"neither"（两端开）
            step (int, optional): 窗口步长（每 step 个数据计算一次）. Defaults to None.
            method (str, optional): 计算方法（"single"=单线程，"table"=矢量化）. Defaults to "single".
            selection (str | list[str] | None, optional): 选择特定列参与计算. Defaults to None.
                - None 表示所有数值列均参与计算

        Returns:
            BtRollingGroupby: 框架自定义的分组滚动窗口对象，支持链式调用后续计算方法

        ### 示例：
        >>> # 1. 按 sector 分组，计算每组的 20 日滚动均值
            self.sector_ma20 = self.data.rolling_groupby(by="sector", window=20).mean()

        >>> # 2. 按多列分组，计算滚动标准差
            self.group_std = self.data.rolling_groupby(
                by=["sector", "exchange"], window=30, min_periods=10
            ).std()

        >>> # 3. 分组滚动四分位数
            self.group_q75 = self.data.rolling_groupby(
                by="symbol", window=50
            ).quantile(0.75)

        >>> # 4. 只对 close 列做分组滚动计算
            self.group_close_mean = self.data.rolling_groupby(
                by="sector", window=20, selection="close"
            ).mean()
        """
        return BtRollingGroupby(
            self,
            by=by,
            window=window,
            min_periods=min_periods,
            center=center,
            win_type=win_type,
            on=on,
            axis=axis,
            closed=closed,
            step=step,
            method=method,
            selection=selection,
        )

    def ewm(
        self,
        com: float | None = None,
        span: float | None = None,
        halflife: float | None = None,
        alpha: float | None = None,
        min_periods: int = 0,
        adjust: bool = False,
        ignore_na: bool = False,
        axis: int | Literal["index", "columns", "rows"] = 0,
    ) -> BtExponentialMovingWindow:
        """## 指数加权移动窗口计算（方法接口）
        - 与Pandas的ewm方法用法完全一致，但返回框架自定义的BtExponentialMovingWindow对象，
        - 支持后续调用mean、std、var等方法时直接返回内置指标对象，
        - 无需手动转换Pandas结果。

        ### 核心场景：
        - 技术指标计算（如指数移动平均线EMA、MACD指标）
        - 风险指标计算（如指数加权波动率、风险价值VaR）
        - 特征工程（如指数平滑成交量、衰减权重计算）

        Args:
            com (float | None, optional): 指定衰减质心. Defaults to None.
                - 定义方式：α = 1/(1+com)，用于计算平滑因子
                - 例如：com=0.5表示最近数据点权重为0.5
            span (float | None, optional): 指定衰减跨度. Defaults to None.
                - 定义方式：α = 2/(span+1)，常用参数（如span=20对应EMA20）
                - 时间跨度，观测值权重降至约1/e的时间窗口
            halflife (float | None, optional): 指定衰减半衰期. Defaults to None.
                - 定义方式：α = 1 - exp(-ln(2)/halflife)
                - 权重降至50%所需的时间周期
            alpha (float | None, optional): 直接指定平滑因子. Defaults to None.
                - 直接设置平滑系数α（0 < α ≤ 1）
                - 值越大，近期数据权重越高
            min_periods (int, optional): 最小非缺失值数量. Defaults to 0.
                - 达到该数量前结果为NaN，默认0（从第一个数据开始计算）
            adjust (bool, optional): 是否使用调整公式. Defaults to False.
                - True：使用调整公式（除以权重和），False：使用递归公式
                - 调整公式更精确但计算稍慢
            ignore_na (bool, optional): 是否忽略缺失值. Defaults to False.
                - True：跳过NaN值计算，False：NaN值会传播到后续结果
            axis (int | str, optional): 计算轴方向. Defaults to 0.
                - 0/"index"：按行方向计算，1/"columns"：按列方向计算

        Returns:
        >>> BtExponentialMovingWindow: 框架自定义的指数加权移动窗口对象，支持链式调用

        ### 示例：
        >>> # 1. 计算12日指数移动平均线（EMA12）
            self.ema12 = self.data.close.ewm(span=12).mean()  # 返回IndSeries对象

        >>> # 2. 计算MACD指标（12日EMA与26日EMA差值）
            self.ema12 = self.data.close.ewm(span=12).mean()
            self.ema26 = self.data.close.ewm(span=26).mean()
            self.macd = self.ema12 - self.ema26  # 返回IndSeries对象

        >>> # 3. 计算指数加权滚动标准差（用于波动率）
            self.ewm_std = self.data.close.ewm(span=20).std()  # 返回IndSeries对象

        >>> # 4. 多列同时计算指数加权均值
            self.ewm_mean = self.data.loc[:, ["open", "close"]].ewm(
                span=10).mean()  # 返回IndFrame对象
        """
        return BtExponentialMovingWindow(
            self,
            com=com,
            span=span,
            halflife=halflife,
            alpha=alpha,
            min_periods=min_periods,
            adjust=adjust,
            ignore_na=ignore_na,
            axis=axis
        )

    def ewm_groupby(
        self,
        by,
        com: float | None = None,
        span: float | None = None,
        halflife: float | None = None,
        alpha: float | None = None,
        min_periods: int = 0,
        adjust: bool = False,
        ignore_na: bool = False,
        axis: int | Literal["index", "columns", "rows"] = 0,
        selection=None,
    ) -> BtExponentialMovingWindowGroupby:
        """## 分组指数加权移动窗口计算（方法接口）

        - 与 Pandas ``groupby(...).ewm()`` 用法对应，但返回框架自定义的 ``BtExponentialMovingWindowGroupby`` 对象，
        - 每个分组内部独立维护指数加权移动窗口，分组间互不干扰，
        - 支持后续调用 ``mean``、``std``、``var``、``corr``、``cov`` 等方法时直接返回内置指标对象，
        - 无需手动转换 Pandas 结果。

        ### 核心场景：
        - 不同行业 / 板块的分组 EMA 计算（按 ``sector`` 分组）
        - 多品种分组的指数加权波动率、相关性统计
        - 分组内 MACD 等衍生指标的批量计算

        Args:
            by: 分组依据，遵循 pandas ``DataFrame.groupby(by)`` 的参数规范：
                - ``str``：单个列名（如 ``"sector"``）
                - ``list[str]``：多个列名（如 ``["sector", "exchange"]``）
                - ``pd.Series``：自定义分组序列
                - 其他 pandas groupby 支持的 ``by`` 参数形式
            com (float | None, optional): 指定衰减质心. Defaults to None.
                - 定义方式：α = 1/(1+com)，用于计算平滑因子
            span (float | None, optional): 指定衰减跨度. Defaults to None.
                - 定义方式：α = 2/(span+1)，常用参数（如 span=12 对应 EMA12）
            halflife (float | None, optional): 指定衰减半衰期. Defaults to None.
                - 定义方式：α = 1 - exp(-ln(2)/halflife)
            alpha (float | None, optional): 直接指定平滑因子. Defaults to None.
                - 直接设置平滑系数 α（0 < α ≤ 1）
            min_periods (int, optional): 每组内最小非缺失值数量. Defaults to 0.
                - 达到该数量前结果为 NaN，默认 0（从每组第一个数据开始计算）
            adjust (bool, optional): 是否使用调整公式. Defaults to False.
                - True：使用调整公式（除以权重和），False：使用递归公式
            ignore_na (bool, optional): 是否忽略缺失值. Defaults to False.
                - True：跳过 NaN 值计算，False：NaN 值会传播到后续结果
            axis (int | str, optional): 计算轴方向. Defaults to 0.
                - 0/"index"：按行方向计算，1/"columns"：按列方向计算
            selection (str | list[str] | None, optional): 选择特定列参与计算. Defaults to None.
                - None 表示所有数值列均参与计算

        Returns:
            BtExponentialMovingWindowGroupby: 框架自定义的分组指数加权移动窗口对象，支持链式调用

        ### 示例：
        >>> # 1. 按 sector 分组，计算每组的 EMA12
            self.sector_ema12 = self.data.ewm_groupby(by="sector", span=12, adjust=False).mean()

        >>> # 2. 按多列分组，计算指数加权标准差
            self.group_ewm_std = self.data.ewm_groupby(
                by=["sector", "exchange"], alpha=0.5
            ).std()

        >>> # 3. 分组 EMA 同时只计算 close 列
            self.group_ema_close = self.data.ewm_groupby(
                by="symbol", span=20, selection="close"
            ).mean()

        >>> # 4. 分组指数加权相关系数
            self.group_corr = self.data.ewm_groupby(
                by="sector", span=30
            ).corr()
        """
        return BtExponentialMovingWindowGroupby(
            self,
            by=by,
            com=com,
            span=span,
            halflife=halflife,
            alpha=alpha,
            min_periods=min_periods,
            adjust=adjust,
            ignore_na=ignore_na,
            axis=axis,
            selection=selection,
        )

    def expanding(
        self,
        min_periods: int = 1,
        axis: int | Literal["index", "columns", "rows"] = 0,
        method: Literal["single", "table"] | None = "single",
    ) -> BtExpanding:
        """## 扩展窗口计算（方法接口）

        - 与Pandas的expanding方法用法完全一致，但返回框架自定义的BtExpanding对象，
        - 支持后续调用mean、sum、std等方法时直接返回内置指标对象，
        - 无需手动转换Pandas结果。

        ### 核心场景：
        - 累积统计计算（如累积最大值、累积最小值）
        - 动态基准计算（如扩展窗口均线、累积收益率）
        - 特征工程（如历史最高价、历史最低价、累积成交量）

        Args:
            min_periods (int, optional): 最小非缺失值数量. Defaults to 1.
                - 达到该数量前结果为NaN，默认1（从第一个数据开始计算）
            axis (Literal[0], optional): 计算轴方向. Defaults to 0.
                - 0：按行方向计算（扩展窗口沿时间轴）
            method (Literal["single", "table"], optional): 计算方法. Defaults to "single".
                - "single"：单列逐个计算，"table"：多列同时计算（性能优化）

        Returns:
        >>> BtExpanding: 框架自定义的扩展窗口对象，支持链式调用后续计算方法

        ### 示例：
        >>> # 1. 计算累积最大值（历史最高价）
            self.cummax = self.data.high.expanding().max()  # 返回IndSeries对象

        >>> # 2. 计算累积最小值（历史最低价）
            self.cummin = self.data.low.expanding().min()  # 返回IndSeries对象

        >>> # 3. 计算扩展窗口均值（从起始点到当前点的均值）
            self.expanding_mean = self.data.close.expanding().mean()  # 返回IndSeries对象

        >>> # 4. 多列同时计算扩展窗口统计量
            self.expanding_stats = self.data.loc[:, ["open", "close"]].expanding(
                min_periods=5).std()  # 返回IndFrame对象

        >>> # 5. 计算累积收益率（使用扩展窗口求和）
            self.cumulative_return = (
                1 + self.data.returns).expanding().prod() - 1
        """
        return BtExpanding(self, min_periods=min_periods, axis=axis, method=method)

    def expanding_groupby(
        self,
        by,
        min_periods: int = 1,
        axis: int | Literal["index", "columns", "rows"] = 0,
        method: Literal["single", "table"] | None = "single",
        selection=None,
    ) -> BtExpandingGroupby:
        """## 分组扩展窗口计算（方法接口）

        - 与 Pandas ``groupby(...).expanding()`` 用法对应，但返回框架自定义的 ``BtExpandingGroupby`` 对象，
        - 每个分组内部独立维护扩展窗口，分组间互不干扰，
        - 支持后续调用 ``mean``、``sum``、``std`` 等方法时直接返回内置指标对象，
        - 无需手动转换 Pandas 结果。

        ### 核心场景：
        - 不同行业 / 板块的累积统计计算（按 ``sector`` 分组）
        - 多品种分组的累积波动率、累积收益率统计
        - 分组内的动态基准计算（如每组独立的扩展窗口均线）

        Args:
            by: 分组依据，遵循 pandas ``DataFrame.groupby(by)`` 的参数规范：
                - ``str``：单个列名（如 ``"sector"``）
                - ``list[str]``：多个列名（如 ``["sector", "exchange"]``）
                - ``pd.Series``：自定义分组序列
                - 其他 pandas groupby 支持的 ``by`` 参数形式
            min_periods (int, optional): 每组内最小非缺失值数量. Defaults to 1.
                - 达到该数量前结果为 NaN，默认 1（从每组第一个数据开始计算）
            axis (Literal[0], optional): 计算轴方向. Defaults to 0.
                - 0：按行方向计算（扩展窗口沿时间轴）
            method (Literal["single", "table"], optional): 计算方法. Defaults to "single".
                - "single"：单列逐个计算，"table"：多列同时计算（性能优化）
            selection (str | list[str] | None, optional): 选择特定列参与计算. Defaults to None.
                - None 表示所有数值列均参与计算

        Returns:
            BtExpandingGroupby: 框架自定义的分组扩展窗口对象，支持链式调用后续计算方法

        ### 示例：
        >>> # 1. 按 sector 分组，计算每组的累积均值
            self.sector_expanding_mean = self.data.expanding_groupby(by="sector").mean()

        >>> # 2. 按多列分组，计算每组的累积标准差
            self.group_std = self.data.expanding_groupby(
                by=["sector", "exchange"], min_periods=5
            ).std()

        >>> # 3. 分组累积最大值（每组独立的历史最高价）
            self.group_cummax = self.data.expanding_groupby(by="symbol").max()

        >>> # 4. 只对 close 列做分组扩展窗口计算
            self.group_close_mean = self.data.expanding_groupby(
                by="sector", selection="close"
            ).mean()
        """
        return BtExpandingGroupby(
            self,
            by=by,
            min_periods=min_periods,
            axis=axis,
            method=method,
            selection=selection,
        )

    # ------------------------------
    # 滚动窗口自定义函数计算
    # ------------------------------
    @tobtind(lib="pta")
    def rolling_apply(self, func: Callable, window: int | pd.Series | np.ndarray | list[int], prepend_nans: bool = True, n_jobs: int = 1, **kwargs) -> IndFrame | IndSeries:
        """## 滚动窗口自定义函数计算（方法接口）
        - 在滚动窗口上应用自定义函数，支持并行计算，结果自动转为框架内置指标对象，
        - 解决Pandas rolling.apply不支持多输出、效率低的问题，适用于复杂指标计算。

        ### 核心优势：
        - 可变窗口支持：窗口大小可为整数（固定窗口）或序列（每个位置自定义窗口大小）
        - 多输出支持：自定义函数可返回多个值（如同时计算均值、标准差），自动生成多线条指标
        - 并行加速：通过n_jobs控制并行线程数，处理大规模数据时提升效率
        - 类型兼容：自动适配一维（IndSeries）/多维（IndFrame）输入，返回对应类型的指标对象

        Args:
            func (Callable): 应用于每个滚动窗口的自定义函数
                - 输入：窗口内的数据（IndSeries→1D np.ndarray，IndFrame→2D np.ndarray或多参数1D数组）
                - 输出：单个值或多个值（如返回(mean, std)生成两个线条）
            window (int | pd.Series | np.ndarray | list[int]): 滚动窗口大小
                - 整数：固定窗口大小（所有位置使用相同窗口）
                - 序列类型（pd.Series/np.ndarray/list）：可变窗口大小
                需满足：长度与主数据一致，且所有元素为正整数
            prepend_nans (bool, optional): 滚动数组长度不足时是否在数组前填充NaN. Defaults to True.
                - True：前window-1个滚动数组长度不足的在其前面填充NaN（默认，符合技术指标习惯）
                - False：滚动数组无Nna值
            n_jobs (int, optional): 并行计算的线程数. Defaults to 1.
                - 1：单线程（默认，避免线程开销）
                - >1：多线程（需func线程安全，适合CPU密集型计算）
                - -1：使用所有可用CPU核心
            **kwargs: 扩展参数（如lines=[]指定线条名称、overlap=True设置主图叠加）

        Returns:
        >>> IndFrame | IndSeries:
            - IndSeries：func返回单个值时（1D结果）
            - IndFrame：func返回多个值时（2D结果）

        ### 示例详解：
        ### 1. 一维输入（IndSeries）→ 多输出（两个线条）
        >>> #自定义函数：计算窗口内收盘价的最小值和最大值
            def calc_window_min_max(close: np.ndarray) -> tuple[float, float]:
                \"\"\"
                close: 窗口内收盘价（1D np.ndarray，长度=window）
                返回：窗口内最小值、最大值
                \"\"\"
                return close.min(), close.max()
            #调用rolling_apply：5日窗口，生成两个线条（"close_min"、"close_max"）
            self.window_min_max = self.data.close.rolling_apply(
                func=calc_window_min_max,
                window=5,
                lines=["close_min", "close_max"],  # 指定线条名称
                overlap=True  # 主图叠加（与K线同图显示）
            )
            #返回结果：IndFrame对象，含"close_min"和"close_max"两列

        ### 2. 多维输入（IndFrame多列）→ 多输出（两个线条）
        >>> #自定义函数：计算窗口内最高价均值和最低价标准差（多参数输入）
            def calc_high_mean_low_std(high: np.ndarray, low: np.ndarray, a: float = 1.) -> tuple[float, float]:
                \"\"\"
                high：窗口内最高价（1D np.ndarray）
                low：窗口内最低价（1D np.ndarray）
                a：自定义参数（示例用，无实际意义）
                返回：最高价均值、最低价标准差
                \"\"\"
                return high.mean(), low.std() * a
            #调用rolling_apply：3日窗口，指定输入列，生成两个线条
            self.high_low_stats = self.data.rolling_apply(
                func=calc_high_mean_low_std,
                window=3,
                lines=["high_mean", "low_std"],  # 线条名称
                # 分别设置叠加（high_mean主图，low_std副图）
                overlap=dict(high_mean=True, low_std=False),
                a=2.  # 传递自定义参数a=2.
            )
            #返回结果：IndFrame对象，含"high_mean"和"low_std"两列

        ### 3. 多维输入（IndFrame）→ 多输出（两个线条，单参数接收）
        >>> #自定义函数：接收2D数组（所有列），计算均值和标准差
            #传递参数为IndFrame以window滚动的数组
            def calc_df_mean_std(df: np.ndarray) -> tuple[float, float]:
                \"\"\"
                df：窗口内所有列的数据（2D np.ndarray，形状=(window, 列数)）
                返回：所有元素的均值、所有元素的标准差
                \"\"\"
                return df.mean(), df.std()
            #调用rolling_apply：3日窗口，输入OHLC四列
            self.ohlc_stats = self.data.loc[:, FILED.OHLC].rolling_apply(
                func=calc_df_mean_std,
                window=3,
                lines=["ohlc_mean", "ohlc_std"],
                overlap=dict(ohlc_mean=True, ohlc_std=False)
            )
            #返回结果：IndFrame对象，含"ohlc_mean"和"ohlc_std"两列
        """
        ...  # minibt.core.CoreFunc.pta_rolling_apply

    # ------------------------------
    # 绘图数据组装（供前端渲染）
    # ------------------------------

    def _get_plot_datas(self, key: str="") -> tuple:
        """## 指标画图数据
        - 组装绘图所需的完整数据（内部方法，供策略绘图模块调用）
        - 整合指标的配置信息（ID、线条、颜色）与数值数据，生成前端可直接渲染的结构化数据，
        - 支持主图叠加/副图显示、多线条分类、自定义水平线等功能。

        Args:
            key (str): 绘图标识（如合约代码、指标分组名，用于区分不同绘图对象）

        Returns:
        >>> tuple: 绘图结构化数据，包含13个元素：
                1. plot_id (int): 绘图分组ID（用于多子图区分）
                2. isplot (list[bool] | bool): 线条显示开关（多线条为列表，单线条为bool）
                3. name (str | list[str]): 指标策略内名称（多分组为列表）
                4. lines (list[str]): 线条标识名（含key前缀，避免重复）
                5. _lines (list[str]): 线条原始名称（无前缀）
                6. ind_names (str | list[str]): 指标类型名（多分组为列表）
                7. overlaps (bool | list[bool]): 主图叠加开关（多分组为列表）
                8. categorys (CategoryString | list[CategoryString]): 绘图分类（多分组为列表）
                9. indicators (np.ndarray): 指标数值数据（形状适配绘图需求）
                10. doubles (list[int] | bool): 多分组线条索引（无分组为False）
                11. _ind_plotinfo (dict): 指标绘图配置（颜色、线型、水平线等）
                12. span (dict): 水平线样式配置（如RSI的20/80分界线）
                13. _signal (dict): 交易信号样式配置（如开多信号的颜色、标记）

        ### 核心逻辑：
        - 1. 基础信息提取：获取plot_id、数据维度、数值数组等基础信息
        - 2. 多分组处理：当部分线条主图叠加、部分副图显示时，拆分为两个分组（主图组+副图组）
        - 3. 数据适配：统一数值数组形状（确保前端渲染兼容），生成线条标识名（避免多指标重名）
        - 4. 配置整合：合并绘图配置（颜色、线型）、水平线、信号样式，生成完整配置字典
        - 5. 自定义指标标记：记录自定义指标的名称与长度，供前端特殊处理
        """
        # 1. 基础信息提取
        if not key:
            key = self.ind_name
        kwargs: Addict = self.get_indicator_kwargs()
        plot_id = kwargs.id["plot_id"]  # 绘图分组ID（多子图区分）
        isndim = kwargs.isMDim  # 是否为多维数据（IndFrame/IndSeries）
        # 数值数组适配：多维直接使用，一维转为(N,1)形状（统一前端处理）
        value = self.values if isndim else self.values.reshape((self.V, 1))
        # 回放模式截断：bokeh回放模式需要数据截断到min_start_length（通过_strategy_replay标记）
        # light_chart回放模式直接调用_get_plot_datas，不受此影响
        if self.strategy_instance and getattr(self.strategy_instance, '_strategy_replay', False):
            value = value[:self.strategy_instance.min_start_length]
        overlap = kwargs.overlap  # 主图叠加配置（bool/dict）
        overlap_isbool = isinstance(overlap, bool)  # 是否为统一叠加开关
        # 线条名称提取：支持Lines对象或普通列表
        # vlines = self.lines.values if hasattr(
        #     self.lines, "values") else self.lines
        vlines = kwargs.lines
        # 2. 多分组处理（部分线条主图、部分副图）
        if not overlap_isbool and len(set(overlap.values())) > 1:
            # 提取主图/副图线条索引
            values = list(overlap.values())
            index1 = [ix for ix, vx in enumerate(values) if vx]  # 主图叠加线条索引
            index2 = [ix for ix, vx in enumerate(values) if not vx]  # 副图线条索引

            # 组装多分组数据（主图组+副图组）
            # 显示开关：按分组提取对应线条的isplot
            isplot = [
                [p for ix, p in enumerate(
                    kwargs.isplot.values()) if ix in index]
                for index in [index1, index2]
            ]
            # 指标名称：每个分组复用当前指标的sname
            name = [kwargs.sname] * 2
            # 线条标识名：添加key前缀（避免多指标重名，如"BTC/USDT_ma5"）
            lines = [
                ["_".join([key, n])
                 for ix, n in enumerate(vlines) if ix in index]
                for index in [index1, index2]
            ]
            # 线条原始名称：无前缀，用于显示
            _lines = [
                [n for ix, n in enumerate(vlines) if ix in index]
                for index in [index1, index2]
            ]
            # 指标类型名：每个分组复用当前指标的ind_name
            ind_names = [key, ] * 2
            # 叠加开关：主图组=True，副图组=False
            overlaps = [True, False]
            # 绘图分类：每个分组复用当前指标的category
            categorys = [str(kwargs.category), ] * 2
            # 数值数据：按分组提取对应列
            indicators = [value[:, index] for index in [index1, index2]]
            # 多分组标记：记录所有线条的索引（用于前端关联）
            doubles = index1 + index2

        # 3. 单分组处理（所有线条统一主图/副图）
        else:
            doubles = False  # 无多分组
            # 统一叠加开关：dict取所有值的逻辑与（全True才主图），bool直接使用
            _overlap = overlap if overlap_isbool else all(overlap.values())
            # 显示开关：多维为列表（每列一个开关），一维为单bool
            isplot = isndim and list(kwargs.isplot.values()) or [
                kwargs.isplot, ]
            # 指标名称：单分组直接使用当前指标的sname
            name = kwargs.sname
            # 线条标识名：主图叠加或多线条时添加key前缀，否则直接用key
            lines = (["_".join([key, n]) for n in vlines]
                     ) if _overlap or len(vlines) > 1 else [key, ]
            # 线条原始名称：直接使用vlines
            _lines = vlines
            # 指标类型名：单分组直接使用key
            ind_names = key
            # 叠加开关：统一开关值
            overlaps = _overlap
            # 绘图分类：单分组直接使用当前指标的category
            categorys = str(kwargs.category)
            # 数值数据：直接使用适配后的value
            indicators = value

        # 4. 自定义指标标记：记录自定义指标所在的图表画图ID（供前端特殊处理）
        if kwargs.iscustom and self.strategy_instance is not None:
            self.strategy_instance._custom_ind_name.update(
                {key: self.plot_id})

        # 5. 绘图配置整合
        # 基础绘图配置：优先取plotinfo的vars，无则取plotinfo自身
        v_plotinfo = self._plotinfo.vars if hasattr(
            self.plotinfo, "vars") else self._plotinfo
        # 蜡烛图特殊处理：添加数据源标识（key）
        if kwargs.category == 'candles':
            v_plotinfo.update(dict(source=key))
        # 完整绘图配置
        _ind_plotinfo = v_plotinfo
        # 水平线配置：从plotinfo获取spanstyle
        span = v_plotinfo.get("spanstyle", {})
        # 信号样式配置：有交易信号时从plotinfo获取signalstyle
        _signal = v_plotinfo.get("signalstyle", {})
        _signal = _signal.vars if hasattr(_signal, "vars") else _signal

        # 返回结构化绘图数据
        return (
            plot_id, isplot, name, lines, _lines, ind_names, overlaps,
            categorys, indicators, doubles, _ind_plotinfo, span, _signal
        )

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
        if options.check_conversion_mode(data, self):
            indicator_kwargs = self.get_indicator_kwargs()
            # 根据数据维度转换为相应的minibt数据结构
            if len(data.shape) > 1:  # 多维数据
                if type(self) in self._get_klinetype() and set(data.columns).issuperset(FILED.ALL):
                    data = data.add_info(**self._klinesetting.symbol_info)
                    return self._kline(data, **indicator_kwargs)
                else:
                    indicator_kwargs["lines"] = list(data.columns)
                    return self._get_indframe()(data.values, **indicator_kwargs)
            else:  # 一维数据
                return self._get_indseries()(data.values, **indicator_kwargs)
        return data

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
            value = np.asarray(value, dtype=self.dtype)
        if self.isMDim:
            pd.DataFrame.__setitem__(self, key, value)
            self.pandas_object.loc[:, key] = value
        else:
            # [:] 全切片赋值且长度不匹配时，重建底层数据（支持空序列 -> 填充数据）
            if isinstance(key, slice) and key == slice(None):
                vlen = len(value) if hasattr(value, '__len__') else 1
                if vlen != len(self):
                    new_series = pd.Series(value, dtype=self.dtype, name=self.name)
                    pd.Series.__init__(self, new_series)
                    self._dataset.pandas_object = pd.Series(
                        self.values, index=self.index, dtype=self.dtype, name=self.name
                    )
                    self._dataset.copy_object = self._dataset.pandas_object.copy()
                    if source_obj is not None:
                        self._dataset.set_source_obj(source_obj)
                    # 跳过后续 set_copy_obj / set_source_obj，直接返回
                    return
                else:
                    pd.Series.__setitem__(self, key, value)
                    self.pandas_object.loc[key] = value
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
            # self._plotinfo.isplot.update({key: True})
            # self._plotinfo.overlap.update({key: False})
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

    def _update_line_data(self, data=None):
        """## 更新 Line 对象的数据"""
        if data is None:
            data = self

        if data.shape != self.shape:
            return

        if not self.isMDim:
            return
        if hasattr(data, "values"):
            data = data.values
        for i, line_field in enumerate(self._plotinfo.line_filed):
            line_obj: Line = getattr(self, line_field)
            line_obj[:] = data[:, i]

    @property
    def iloc(self) -> MinibtILocIndexer:
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
    def loc(self) -> MinibtLocIndexer:
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

        示例：
        >>> df.loc['viper']          # 选择行标签 'viper'（返回 IndSeries 类型）
        >>> df.loc[df['shield']>6]   # 选择 'shield' 列值大于 6 的行（返回 IndFrame 类型）
        """
        return MinibtLocIndexer("loc", self)

    def btplot(
            self,
            include: Literal["all", "last"] = "all",
            black_style: bool = False,
            open_browser: bool = False,
            plot_cwd: str = "",
            plot_name: str = "",
            save_plot: bool = False,
            **kwargs) -> None:
        """
        ##  Bokeh生成的交互式金融行情技术分析图表
        - 此函数自动检测数据格式（OHLCV、时间序列或指标）并使用内置技术分析工具
        - 和专业样式创建相应的交互式可视化图表。

        ### 📘 **文档参考**:
        - https://www.minibt.cn/minibt_basic/1.19minibt_interactive_financial_charting_with_btplot/

        ### 参数

        - **include**: 多指标显示模式
          - `"all"`: 显示计算链中的所有相关指标
          - `"last"`: 仅显示最终的指标结果
        - **black_style**: 启用暗黑主题，在低光环境下提供更好的视觉舒适度
        - **open_browser**: 强制在网页浏览器中打开（默认自动检测Jupyter环境）
        - **plot_cwd**: 保存图表文件的自定义目录路径（默认使用strategy/plots）
        - **plot_name**: 保存图表的自定义文件名（默认：'btplot.html'）
        - **save_plot**: 自动将图表保存为HTML文件，便于分享或文档记录
        - `**kwargs`: 传递额外参数到指标/KLine实例，用于在绘图前设置属性。

          常用参数如下：

          - `kline` (KLine | list[KLine] | tuple[KLine]): 指定 K 线数据源。
            支持单个 KLine 或 多个 KLine（list/tuple）。
              - 单个 KLine：作为主图（OHLC 蜡烛图）显示。
              - 多个 KLine：第一个作为主图，后续 KLine 作为副图蜡烛图依次排列
                （位于成交量图之后、指标图之前），共享 x 轴联动缩放。
            例如 `kline=kline_a` 或 `kline=[kline_v, kline_pp]`

            *当指标无法通过计算链自动追溯到 K 线时，可通过此参数显式指定。*
          - `height` (int): 图表绘制高度（像素），需 ≥ 20。例如 `height=500`
          - `candle_style` (CandleStyle | dict): 蜡烛图样式配置，可设置
            `bull_color`/`bear_color` 修改K线涨跌颜色。
            例如 `candle_style={'bull_color': '#26a69a', 'bear_color': '#ef5350'}`
          - `span_style` (float | SpanStyle | list): 添加水平参考线。
            可以是单个数值（如 `span_style=50.0`）、单个 `SpanStyle` 对象、
            或 `SpanStyle` 列表。`SpanStyle` 支持 location/color/dash/width 等属性。
            例如 `span_style=[SpanStyle(location=50, color='red', dash='dashed')]`
          - `span_location` (float | list): 水平线位置（简化写法），效果等同于 `span_style`
          - `span_color` (str | list): 水平线颜色（简化写法）
          - `span_dash` (str | list): 水平线型（简化写法），可选 `'solid'`/`'dashed'`/`'dotted'`等
          - `span_width` (float | list): 水平线宽度（简化写法）
          - `bull_color` (str): K线上涨颜色（快速设置）
          - `bear_color` (str): K线下跌颜色（快速设置）
          - `follow` (bool): 指标是否跟随主图显示（默认 True）
          - `plot_index` (list[int]): 绘图时的索引范围，用于只显示部分数据

          注意：`kwargs` 会通过 `indicator(**kwargs)` 传递，对KLine对象会
          重新构造实例应用属性，对指标对象会调用`upsample`进行上采样过滤。

        ### 功能特性:
        - 智能OHLCV检测，提供K线图和成交量柱状图
        - 交互式缩放、平移和悬停提示，显示精确数据值
        - 自动指标叠加，采用专业配色方案
        - 十字准星工具，用于精确坐标跟踪
        - 图例控制，可显示/隐藏单个指标
        - 响应式设计，适应容器尺寸
        - 支持暗黑/明亮主题，满足不同视觉偏好
        - 多环境支持（Jupyter和独立浏览器）
        - 自动处理缺失值和数据缺口

        ### 返回:
        >>> None: 在Jupyter notebook或网页浏览器中显示交互式图表

        ### 示例:
        >>> # 基础OHLCV图表（含成交量）
        >>> bd = KLine(df, height=400)
        >>> bd.btplot()

        >>> # 复杂指标链可视化
        >>> bd.tradingview.G_Channels().avg.ema().ebsw().btplot(include="all",black_style=True)

        >>> # 交易信号图表（暗黑主题）
        >>> bd.tradingview.UT_Bot_Alerts().btplot(black_style=True)

        >>> # 仅显示最终指标结果
        >>> bd.close.sma().ema().btplot("last")
        """
        self.BtPlot()(self, include, black_style, open_browser, plot_cwd, plot_name, save_plot,**kwargs)

    def concat(
        self,
        objs: Iterable[pd.Series | pd.DataFrame] | Mapping[Hashable, pd.Series | pd.DataFrame],
        *,
        axis: int | Literal["index", "columns", "rows"] = 0,
        join: str = "outer",
        ignore_index: bool = False,
        keys: Iterable[Hashable] | None = None,
        levels=None,
        names: list[Hashable] | None = None,
        verify_integrity: bool = False,
        sort: bool = False,
        copy: bool | None = None,
        **kwargs
    ) -> KLine | IndFrame | IndSeries:
        """
        ## minibt框架的连接函数 - 合并多个pandas对象或minibt对象

        ### 功能概述：
        - 1. 将当前对象与传入对象列表进行连接
        - 2. 自动处理minibt对象到pandas对象的转换
        - 3. 根据连接结果决定返回pandas对象还是minibt对象
        - 4. 保持minibt框架的指标属性

        Args：
            self : IndFrame | IndSeries
                当前minibt对象（调用者）
            objs : Iterable[pd.Series |
                pd.DataFrame] | Mapping[Hashable, pd.Series | pd.DataFrame]
                待连接的对象集合，可以是：
                - pandas Series或DataFrame的可迭代对象
                - minibt的IndSeries或IndFrame对象
                - 包含哈希键到对象的映射
            axis : Axis = 0
                连接轴方向：
                - 0: 沿行方向连接（垂直堆叠）
                - 1: 沿列方向连接（水平拼接）
            join : str = "outer"
                连接方式：
                - "outer": 外连接，取所有索引的并集（默认）
                - "inner": 内连接，取所有索引的交集
                - "left": 左连接，以左侧对象索引为准
                - "right": 右连接，以右侧对象索引为准
            ignore_index : bool = False
                是否忽略原始索引：
                - True: 忽略原始索引，生成新索引0,1,2...
                - False: 保留原始索引（默认）
            keys : Iterable[Hashable] | None = None
                用于创建多层索引的键序列
                为连接后的数据添加外层索引标识
            levels : list | None = None
                多层索引的层级定义
            names : list[Hashable] | None = None
                多层索引各层级的名称
            verify_integrity : bool = False
                是否验证索引重复性：
                - True: 检查结果索引是否有重复
                - False: 不检查（默认）
            sort : bool = False
                是否对结果索引排序：
                - True: 对结果索引进行排序
                - False: 保持原有顺序（默认）
            copy : bool | None = None
                是否复制数据：
                - True: 总是复制数据
                - False: 尽可能不复制数据
                - None: 默认行为

        Kwargs :
            - 传递给minibt对象构造函数的其他关键字参数

        ### 返回：
        >>> IndFrame | IndSeries 
            - 满足条件时返回IndFrame（多维）或IndSeries（一维）
            - 不满足条件时返回原始pandas对象

        ### 处理流程：
        - 1. 构建连接对象列表：将当前对象self添加到待连接对象最前面
        - 2. 对象转换：将所有minibt对象转换为底层pandas对象
        - 3. pandas连接：调用pandas.concat进行实际连接操作
        - 4. 条件检查：使用options.check_conversion_mode检查是否需要转换回minibt对象
        - 5. 类型判断：根据数据维度决定创建IndFrame还是IndSeries
        - 6. 属性继承：从当前对象继承指标属性（如lines等）
        """

        # 步骤1：构建连接对象列表，将当前对象self放在最前面
        objs = [self, *objs]

        # 步骤2：对象转换 - 将所有minibt对象转换为底层pandas对象
        objs = [obj.pandas_object if hasattr(
            obj, "pandas_object") else obj for obj in objs]

        # 步骤3：使用pandas的原生concat函数进行数据连接
        data = pd.concat(objs, axis=axis, join=join, ignore_index=ignore_index, keys=keys,
                         levels=levels, names=names, verify_integrity=verify_integrity, sort=sort, copy=copy)

        # 步骤4：检查是否需要将结果转换回minibt对象
        if not options.check_conversion_mode(data, self):
            # 不满足转换条件，直接返回pandas对象
            return data

        # 步骤5：判断连接结果的维度
        isMDim = len(data.shape) > 1  # 是否为多维数据（DataFrame）

        # 步骤6：从当前对象获取指标属性，用于构建新的minibt对象
        indicator_kwargs = self.get_indicator_kwargs(**kwargs)

        # 步骤7：根据维度创建相应的minibt对象
        if isMDim:
            # 多维数据 -> 创建IndFrame
            if all(field in data.columns for field in FILED.ALL):
                if hasattr(self, "_klinesetting"):
                    data = data.add_info(**self._klinesetting.symbol_info)
                return self._kline(data, **kwargs)
            indicator_kwargs["lines"] = list(data.columns)  # 设置列名为lines
            return self._get_indframe()(data.values, **indicator_kwargs)
        else:
            # 一维数据 -> 创建IndSeries
            return self._get_indseries()(data.values, **indicator_kwargs)


