from __future__ import annotations
from .base import StrategyBase
from numpy.lib.stride_tricks import as_strided
from ..utils import (reduce, Addict, storeData,
                     FILED, time_to_datetime,
                     BtAccount, np, BASE_DIR,
                     ffillnan, Base, Literal,
                     abc, KLinesSet, # execute_once,
                     partial, IndSetting,
                     timedelta, Iterable, loadData,
                     BtIndicatorDataSet, pd,
                     Config as btconfig,
                     FilteredOutputRedirector,
                     options, Any)
from ..indicators import (KLine, Line, IndSeries, IndFrame,
                          BtIndType, KLineType)


class StrategyMeta(type):
    """
    ## 策略元类（继承自type）
    - 核心职责是对Strategy的子类进行统一管理，
    - 包括强制初始化流程、保存用户自定义逻辑、避免类定义阶段的错误，
    - 确保所有策略子类都遵循"先执行Strategy父类初始化，再执行用户自定义逻辑"的规范。
    """

    def __new__(cls, name, bases, attrs):
        """
        ### 元类的核心方法：
        - 负责创建目标类（如Strategy、Strategy0(子类)、Strategy1(子类)）的类对象，
        - 在类被定义时触发，用于修改类的属性（此处主要是重写__init__方法）。

        ### 参数说明：
        - cls: 元类本身（即StrategyMeta）
        - name: 待创建的类的名称（如"Strategy"、"Strategy0"、"Strategy1"）
        - bases: 待创建的类的父类列表（如Strategy的父类是[Base]，Strategy0的父类是[Strategy]）
        - attrs: 待创建的类的属性字典（包括方法，如__init__、_strategy_init等）

        ### 返回：
            创建完成的类对象
        """
        # 关键逻辑1：跳过对Strategy类本身的处理，仅需处理其子类。
        if name == "Strategy":
            # 直接调用父类（type）的__new__创建Strategy类，不做额外修改
            return super().__new__(cls, name, bases, attrs)

        # 2. 先判断是否已被处理过（避免复制类/间接子类重复触发）
        if attrs.get("_is_strategy_processed"):
            return super().__new__(cls, name, bases, attrs)

        # -------------------------- 区分直接子类/间接子类 --------------------------
        # 定义两个标记：
        is_direct_subclass = False  # 直接子类：父类列表中存在Strategy
        is_indirect_subclass = False  # 间接子类：父类是Strategy的子类，但父类不是Strategy

        # 遍历父类列表，判断类型
        for base in bases:
            base_name = base.__name__
            if base_name == "Base":  # 排除Base类，不干扰判断
                continue

            # 判断是否为「直接子类」（父类就是Strategy）
            if base_name == "Strategy":
                is_direct_subclass = True
                break  # 找到一个直接父类即可，无需继续遍历

            # 判断是否为「间接子类」（父类是Strategy的子类，但不是Strategy本身）
            if issubclass(base, Strategy) and base_name != "Strategy":
                is_indirect_subclass = True

        # -------------------------- 按子类类型执行不同逻辑 --------------------------
        # 情况1：是直接子类（如owen继承Strategy）→ 执行完整改造
        if is_direct_subclass:
            # print(f"[元类处理] {name}（Strategy直接子类）→ 执行__init__改造")

            # 保存用户自定义的__init__（原逻辑）
            original_init = attrs.get("__init__")

            # 定义新的__init__：先执行Strategy父类初始化，再自动调用用户逻辑
            def new_init(self, *args, **kwargs):
                Strategy.__init__(self, *args, **kwargs)  # 父类初始化

            # 保存原__init__为_strategy_init，替换新__init__，添加已处理标记
            if original_init:
                attrs["_strategy_init"] = original_init  # execute_once(original_init)
            attrs["__init__"] = new_init
            attrs["_is_strategy_processed"] = True  # 标记为已处理

        # 情况2：是间接子类（如owen1继承owen）→ 完全跳过改造，直接继承父类
        elif is_indirect_subclass:
            # 关键：不修改__init__和_strategy_init，直接继承父类的方法
            # 同时添加已处理标记，避免后续误触发
            attrs["_is_strategy_processed"] = True

        # 情况3：非Strategy相关类→ 跳过处理
        else:
            ...

        # 返回最终创建的类对象
        return super().__new__(cls, name, bases, attrs)


class Strategy(Base, StrategyBase, metaclass=StrategyMeta):
    """## 量化交易策略核心基类（继承Base基础类、StrategyBase策略接口类和StrategyMeta元类）

    ### 📘 **文档参考**:
    - 类简介：https://www.minibt.cn/minibt_basic/1.8minibt_strategy_class_guide/

    ### 核心定位：
    - 统一封装量化交易的回测、实盘、参数优化、强化学习（RL）等全流程能力，提供标准化接口供用户自定义策略逻辑

    ### 主要功能：
    - 1. 策略生命周期管理：包含初始化、数据准备、回测运行、结果输出、实盘启动等完整流程
    - 2. 多周期数据处理：支持 K 线周期转换（resample 重采样、replay 数据回放），解决多周期策略的数据对齐问题
    - 3. 指标与数据管理：自动收录自定义指标（IndSeries/IndFrame）与 K 线数据（KLine），维护数据一致性
    - 4. 参数优化：支持策略参数批量优化，自动计算优化目标（如收益率、夏普比率）
    - 5. 强化学习集成：对接 elegantrl 框架，支持 RL 智能体训练、加载与预测，含数据增强、特征处理能力
    - 6. 实盘与回测兼容：统一接口适配回测（基于 BtAccount 虚拟账户）与实盘（基于天勤 TQApi 账户）

    ### **使用示例：**
    >>> from minibt import *
        class MA(Strategy):
            params = dict(length1=10, length2=20)  # 策略参数
            def __init__(self):
                # 1. 获取回测数据
                self.kline = self.get_kline(LocalDatas.test)
                # 2. 计算指标
                self.ma1 = self.kline.close.sma(self.params.length1)
                self.ma2 = self.kline.close.sma(self.params.length2)
                # 3. 定义交易信号
                self.long_signal = self.ma1.cross_up(self.ma2)  # MA1上穿MA2：开多信号
                self.short_signal = self.ma2.cross_down(
                    self.ma1)  # MA2下穿MA1：开空信号
            def next(self):
                # 无持仓时：根据信号开仓
                if not self.kline.position:
                    if self.long_signal.new:
                        self.kline.buy()  # 开多
                    elif self.short_signal.new:
                        self.kline.sell()  # 开空
                # 有多头持仓时：空头信号平仓
                elif self.kline.position > 0 and self.short_signal.new:
                    self.sell()  # 平多
                # 有空头持仓时：多头信号平仓
                elif self.kline.position < 0 and self.long_signal.new:
                    self.buy()  # 平空
        if __name__ == "__main__":
            bt = Bt(auto=True)  # 初始化回测引擎（自动加载策略）
            bt.run()  # 启动策略
    """

    def __init__(self: Strategy, *args, **kwargs):
        """
        ## 策略实例初始化（入口方法）

        ### 核心作用：
        - 注册实例到全局管理集合、初始化配置/参数、绑定自定义属性

        Args:
            *args: 额外位置参数（预留扩展）
            **kwargs: 额外关键字参数（用于动态设置属性，如自定义指标、数据）

        ### 逻辑说明：
        1. 提取策略类名，将当前实例注册到全局策略实例集合（便于框架管理）
        2. 初始化策略配置：优先使用类自定义config，无则用框架默认btconfig
        3. 初始化策略参数：转换为Addict类型（支持属性式访问，如self.params.length1）
        4. 绑定kwargs属性：将用户传入的自定义数据/指标设为实例属性
        """
        strategy_class_name = self.__class__.__name__
        global_strategy_instances = self.__class__._strategy_instances  # 全局策略实例管理集合
        # 使用「类名 + _sid」作为唯一键，避免多策略同名类覆盖（如多个owen策略）
        sid = kwargs.get('_sid', 0) if isinstance(kwargs, dict) else 0
        global_strategy_instances.add_data(f"{strategy_class_name}_{sid}", self)  # 注册当前实例

        # 初始化策略配置（确保类型为btconfig）
        self.config = self.config if isinstance(
            self.config, btconfig) else btconfig()
        if self.config.logger_params_is_change:
            self.logger.set_params(self.config.logger_params)
        # 初始化策略参数（支持属性式访问）
        self.params = Addict(self.params) if isinstance(
            self.params, dict) else Addict()

        # 动态绑定用户自定义属性（如self.custom_indicator = kwargs["custom_indicator"]）
        kwargs.update(dict(_data_patching=options._data_patching))
        for attr_name, attr_value in kwargs.items():
            setattr(self, attr_name, attr_value)
            
        if self._isoptimize:
            self._call_func = self._optimize_single_param_set       # 1. 参数优化模式
        elif self._is_live_trading:
            self._call_func = self._execute_live_trading            # 2. 实盘模式
        elif self._strategy_replay:
            self._call_func = self._strategy_replay_iteration       # 3. 策略回放
        else:
            self._call_func = self._start_strategy_run              # 4. 回测模式（默认）

    def _prepare_before_strategy_start(self) -> Strategy:
        """
        ## 策略启动前的准备流程（聚合基础初始化步骤）
        ### 核心作用：
        - 按顺序执行「基础组件初始化→用户自定义初始化→数据初始化→启动钩子」

        ### 执行步骤：
        1. init_basic_components_before_start()：初始化基础组件（数据集合、账户）
        2. _strategy_init()：用户自定义初始化逻辑（子类重写的__init__核心逻辑）
        3. init_strategy_data()：初始化策略数据（检查数据、历史记录、指标映射）
        4. start()：策略启动钩子（用户可重写，用于指标预计算等）
        """
        self._init_basic_components_before_start()  # 1. 基础组件初始化
        self._strategy_init()                       # 2. 用户自定义初始化（子类逻辑）
        self._init_strategy_data()                  # 3. 策略数据初始化
        self.start()                                # 4. 启动钩子（用户可重写）
        return self

    def _start_strategy_run(self: Strategy,*args, **kwargs) -> Strategy:
        """
        ## 策略运行主流程（回测/实盘统一入口）
        ### 核心作用：
        - 调度策略全生命周期（准备→RL逻辑→交易循环→结果整理）

        Args:
            **kwargs: 启动参数（如RL训练的迭代次数、学习率等）

        ### 执行步骤：
        1. 准备阶段：调用prepare_before_strategy_start()完成基础初始化
        2. RL逻辑：若启用RL，执行「随机策略测试→Agent训练→Agent加载」
        3. 交易循环：调用_execute_core_trading_loop()执行核心回测/实盘逻辑
        4. 结果整理：调用prepare_plot_data()整理可视化所需数据
        5. 返回实例：支持链式调用（如strategy._start_strategy_run().output_results()）
        """
        # 1. 策略启动前准备
        self._prepare_before_strategy_start()

        # 2. 强化学习（RL）专属逻辑
        if self.rl:
            # 2.1 随机策略测试（验证RL环境有效性）
            if self._rl_config.random_policy_test:
                self.random_policy_test()
                return self
            # 2.2 训练RL智能体（若开启训练模式）
            if self._rl_config.train:
                self.train_agent(**kwargs)
            # 2.3 加载训练好的RL智能体
            self.load_agent()

        # 3. 执行核心交易循环（回测/实盘通用）
        self._execute_core_trading_loop()
        # 4. 整理绘图数据（后续可视化用）
        self._get_plot_datas()

        return self
    
    def __call__(self: Strategy, *args, **kwds) -> Strategy:
        """
        ## 策略实例调用入口（模式分发器）
        ### 核心作用：
        - 根据策略运行模式，分发到对应逻辑（参数优化/实盘/回测）

        Args:
            *args: 位置参数（参数优化模式下传入「单组待优化参数」）
            **kwds: 关键字参数（启动配置，如绘图开关、训练参数）

        ### 模式分支：
        1. 参数优化模式（_isoptimize=True）：调用_optimize_single_param_set()
        2. 实盘模式（_is_live_trading=True）：调用_execute_live_trading()
        3. 策略回放模式（_strategy_replay=True）：调用_strategy_replay_iteration()
        4. 回测模式（默认）：调用_start_strategy_run()
        """
        return self._call_func(*args, **kwds)

    def _optimize_single_param_set(self: Strategy, params: dict, is_maximize: bool, target_metrics: Iterable,**kwargs):
        """
        ## 单组参数优化逻辑（参数优化的核心单元）
        ### 核心作用：
        - 用指定参数组执行回测，计算并返回优化目标值（如收益率、夏普比率）

        Args:
            params (dict): 单组待优化参数（如{"length1":15, "length2":30}）
            is_maximize (bool): 优化目标是否最大化（如收益率→True，风险→False）
            target_metrics (Iterable): 优化目标指标（如["total_profit", "sharpe_ratio"]）

        ### 执行步骤：
        1. 应用参数组：将待优化参数设为当前策略参数
        2. 重置优化状态：清空历史交易记录、仓位等（避免参数间干扰）
        3. 重置RL环境：若启用RL，重置环境状态（避免训练残留）
        4. 初始化策略：调用启动钩子→用户自定义初始化→执行回测
        5. 计算目标值：返回优化指标结果（供参数优化器筛选最优参数）
        """
        # 1. 应用当前待优化参数组
        self.params = params
        # 2. 重置优化状态（清空历史记录、仓位等）
        self._reset_optimization_state()
        # 3. 若启用RL，重置环境（避免前一组参数的训练残留）
        if self.rl and self.env:
            self.env.reset()

        # 4. 初始化并执行回测
        self.start()                      # 启动钩子（指标预计算）
        self._strategy_init()             # 应用新参数重新初始化策略
        self._execute_core_trading_loop()  # 执行回测循环

        # 5. 计算并返回优化目标值
        return self._calculate_optimization_targets(is_maximize, target_metrics)

    def _init_basic_components_before_start(self):
        """
        ## 策略启动前的基础组件初始化
        ### 核心作用：
        - 创建「数据集合、指标集合」，并初始化账户（区分回测/实盘）

        ### 初始化内容：
        1. 数据集合：_btklinedataset（管理所有K线数据KLine实例）
        2. 指标集合：_btindicatordataset（管理所有自定义指标实例）
        3. 账户：
           - 实盘/快速实盘：从TQApi获取真实账户（self._api.get_account()）
           - 回测：创建虚拟账户（BtAccount，初始资金取自self.config.value）

        ### 幂等性设计：
        - 由于 _start_strategy_run 可能在 bt.run() 中被调用多次
          （第一次 s(_sid=i,...) 通过 metaclass.__call__ 触发，
           第二次 s() 再次触发），_btklinedataset 和 _btindicatordataset
           需要保留首次调用时通过 _strategy_init → get_kline 添加的数据，
           否则第二次调用时 _init_strategy_data 的断言会因空 OrderedDict 而失败。
        """
        # —— K线数据集合（保留已有数据，避免二次调用时覆盖）——
        if not hasattr(self, '_btklinedataset'):
            self._btklinedataset = KLinesSet()
        elif not self._btklinedataset:
            # 防御：已存在属性但内容为空（如首次调用被异常中断），重新初始化
            self._btklinedataset = KLinesSet()

        # —— 指标数据集合（同理保留已有数据）——
        if not hasattr(self, '_btindicatordataset'):
            self._btindicatordataset = BtIndicatorDataSet()
        elif not self._btindicatordataset:
            # 防御：已存在属性但内容为空（如首次调用被异常中断），重新初始化
            self._btindicatordataset = BtIndicatorDataSet()

        # 初始化账户（实盘vs回测）
        if self._is_live_trading or self.quick_live:
            self._account = self._api.get_account()  # 实盘：天勤TQ账户
        else:
            # 回测：虚拟账户（初始资金=config.value，日志开关=config.islog）
            self._account: BtAccount = BtAccount(
                self, self.config.value, self.config.islog)
        self.on_close = self.config.on_close

    def _init_strategy_data(self):
        """
        ## 策略数据初始化（确保数据可用性与一致性）
        ### 核心作用：
        - 检查数据完整性、初始化历史记录、准备交易组件

        ### 执行步骤：
        1. 快速启动预留逻辑：若启用quick_start且无数据，预留快速初始化入口
        2. 数据完整性检查：确保已添加K线数据（否则报错）
        3. 账户历史初始化：若设置最小启动长度，初始化账户历史记录
        4. 指标映射初始化：创建自定义指标名称映射（用于后续绘图）
        5. 结果容器初始化：创建回测结果列表（存储每笔交易/周期结果）
        6. 实盘持仓记录：记录实盘初始持仓（方向、开仓价）
        7. 止损止盈检查：判断是否有K线数据绑定止损止盈器
        8. 数据量记录：记录K线数据总数（用于循环控制）
        9. 策略逻辑绑定：若重写next方法且非RL模式，绑定交易循环逻辑
        """
        # 1. 快速启动模式预留逻辑（用户可根据需求启用）
        if self.quick_start and not self._datas:
            ...  # 预留快速初始化代码（原逻辑保留，无功能修改）

        # 2. 数据完整性检查：必须先通过adddata()/get_kline添加K线数据
        #    KLinesSet(OrderedDict) 空时为 falsy，此处显式校验 num > 0 避免依赖隐式 bool
        assert (hasattr(self, '_btklinedataset')
                and self._btklinedataset is not None
                and self._btklinedataset.num > 0), \
            '请先通过adddata()方法添加K线数据,或在策略初始化时使用self.get_kline(LocalDatas.test)'

        # 3. 账户历史记录初始化（回测模式下，若设置最小启动长度）
        self.min_start_length = max(0,self.min_start_length)
        if self.min_start_length > 0 and not self._is_live_trading:
            self._account._init_history(self.min_start_length)

        # 4. 初始化自定义指标名称映射（用于后续绘图时匹配指标名）
        self._custom_ind_name = {}
        # 5. 初始化回测结果容器（存储每笔交易结果或每个周期的账户状态）
        self._results = []

        # 6. 实盘模式：记录初始持仓状态（方向+开仓价）
        if self._is_live_trading:
            initial_position_records = []
            for _, kline in self._btklinedataset.items():
                current_position = kline.position
                position_direction = current_position.pos  # 持仓方向：1=多，-1=空，0=无
                # 记录开仓价（多头取多单开仓价，空头取空单开仓价，无持仓取0）
                open_price = (current_position.open_price_long if position_direction > 0
                              else current_position.open_price_short if position_direction < 0
                              else 0.0)
                initial_position_records.append(
                    [position_direction, open_price])
            self._init_trades = [self.sid, initial_position_records]

            # 6.1 策略id标记存放地址，用于实时图表更新
            self._id_dir = f"{BASE_DIR}/liveplot/id" if self._is_live_trading else f"{BASE_DIR}/liveplot/replay/id"

        # 7. 检查是否绑定止损止盈器（_isstop：True=有止损止盈，False=无）
        has_stop_loss_take_profit = [
            True if data.stop else False for data in self._btklinedataset.values()]
        self._isstop = any(has_stop_loss_take_profit)

        # 8. 记录合约个数
        self._datas_num = self._btklinedataset.num

        # 9. 绑定交易循环逻辑：非RL模式且重写next时，将step指向next（循环执行）
        # 核心循环函数其实为step函数，非next函数（考虑到强化学习训练时循环函数为step）
        if self._is_method_overridden("next") and (not self.rl):
            self.step = self.next

        if hasattr(self, "_baostock"):
            with FilteredOutputRedirector():
                self._baostock.logout()

        if hasattr(self, "_pytdx"):
            self._pytdx.close()

    def _custom(self) -> None:
        """
        ## 自定义指标绘图数据处理方法
        ### 核心作用：
        - 更新自定义指标的绘图配置（如数据填充、重叠显示），确保与主图/K线数据对齐

        ### 逻辑说明：
        1. 遍历自定义指标名称映射（_custom_ind_name），获取指标数据（IndSeries/IndFrame）
        2. 处理指标周期转换：若指标为大周期（isresample=True），调用_multi_indicator_resample转换为主周期
        3. 处理重叠显示配置：若overlap为字典（按列配置），拆分显示/隐藏的指标列索引
        4. 更新绘图数据：将处理后的指标数据更新到_plot_datas（绘图数据源）
        """
        if self._custom_ind_name:
            for k, index in self._custom_ind_name.items():
                # 获取自定义指标数据（IndSeries或IndFrame）
                v: IndFrame | IndSeries = getattr(self, k)
                _id = v.id  # 指标ID（用于绘图数据定位）
                # 判断是否为多列指标（IndFrame）
                is_IndFrame = len(v.shape) > 1

                # 提取指标数据（处理IndFrame/IndSeries的维度差异）
                value = v._custom_data if is_IndFrame else v._custom_data.reshape(
                    (len(v), 1))

                # 大周期指标转换：将大周期指标数据转换为主周期长度
                if v.isresample:
                    _value = self._multi_indicator_resample(v)
                    # 重新创建指标实例（确保格式正确）
                    setattr(
                        self, k,
                        IndFrame(
                            _value, **v.ind_setting) if is_IndFrame else IndSeries(_value[:, 0], **v.ind_setting)
                    )
                    # 主图显示大周期指标：更新数据与ID
                    if v._ismain and not self.tq:
                        value = _value
                        _id = v.isresample - 1

                # 处理重叠显示配置（overlap：是否与主图重叠）
                overlap = v._overlap
                overlap_isbool = isinstance(overlap, bool)
                # 获取当前指标的绘图数据
                datas = list(self._plot_datas[2][_id][index])

                # 按列配置重叠显示：拆分显示（True）与隐藏（False）的列索引
                if not overlap_isbool and len(set(overlap.values())) > 1:
                    values = list(overlap.values())
                    index1 = [ix for ix, vx in enumerate(
                        values) if vx]  # 显示的列索引
                    index2 = [ix for ix, vx in enumerate(
                        values) if not vx]  # 隐藏的列索引
                    datas[7] = [value[:, index] for index in [index1, index2]]
                else:
                    datas[7] = value  # 整体配置：直接赋值数据

                # 更新绘图数据
                self._plot_datas[2][_id][index] = datas

    def _multi_indicator_resample(self, data: IndSeries | IndFrame) -> np.ndarray:
        """
        ## 多周期指标周期转换方法
        ### 核心作用：
        - 将大周期指标数据（如900秒）转换为主周期长度（如300秒），确保时间对齐与数据完整性

        Args:
            data (Union[IndSeries, IndFrame]): 待转换的大周期指标数据

        Returns:
            np.ndarray: 转换后主周期长度的指标数据（前向填充NaN）

        ### 流程说明：
        1. 获取指标ID与主周期数据：确定指标对应的主周期K线数据（main_data）
        2. 数据格式统一：将IndSeries转换为IndFrame，添加datetime列用于时间对齐
        3. 创建主周期时间框架：生成与主周期K线时间一致的空数据框（datas）
        4. 数据合并：通过datetime列合并大周期指标与主周期时间框架，填充NaN
        5. 数据清洗：排序、去重、前向填充NaN，返回处理后的数据
        """
        _id = data.data_id  # 指标对应的主周期数据ID
        rid = data.resample_id  # 指标的原始周期ID
        main_data = self._btklinedataset[rid].pandas_object  # 主周期K线数据

        # 若指标长度与主周期一致，无需转换
        if len(data) == len(main_data):
            return

        # 大周期指标对应的原始K线数据
        multi_data = self._btklinedataset[_id].pandas_object
        # 判断是否为单列指标（IndSeries）
        isIndSeries = isinstance(data, IndSeries)

        # 数据格式统一：IndSeries→IndFrame，添加datetime列
        raw_cols = isIndSeries and data.lines or list(data.columns)
        data = pd.DataFrame(
            data.values, columns=raw_cols) if isIndSeries else data
        data['datetime'] = multi_data.datetime.values  # 添加大周期指标的时间列
        data = data[['datetime'] + raw_cols]  # 重新排列列：datetime在前，指标列在后

        # 创建主周期时间框架（空数据，仅含主周期datetime）
        cols = list(data.columns)
        datetime = main_data["datetime"].values
        datas = pd.DataFrame(
            np.full((len(datetime), data.shape[1]), np.nan), columns=cols
        )
        datas['datetime'] = datetime

        # 合并数据：通过datetime对齐大周期指标与主周期时间
        df = pd.merge(datas, data, how='outer', on='datetime')

        # 填充合并后的NaN（优先用主周期数据，无则用大周期数据）
        for col in cols[1:]:
            df[col] = df.apply(
                lambda x: x[f'{col}_y'] if pd.isna(
                    x[f'{col}_x']) else x[f'{col}_x'],
                axis=1
            )

        # 数据清洗：排序、去重、前向填充
        df = df[cols]
        df.sort_values(by=cols[:2], na_position='first',
                       ignore_index=True, inplace=True)
        df.drop_duplicates('datetime', keep='last',
                           ignore_index=True, inplace=True)

        # 确保数据长度与主周期一致
        if len(datetime) != df.shape[0]:
            df = df[~pd.isna(df.datetime)]
            df.reset_index(drop=True, inplace=True)

        # 前向填充NaN（大周期指标在主周期内的数值延续）
        return df.ffill()[cols[1:]].values

    def __multi_data_resample(self, data: KLine, if_replay: bool = False) -> pd.DataFrame:
        """
        ## 多周期K线数据转换方法
        ### 核心作用：
        - 将大周期K线数据转换为主周期长度，用于多周期策略的回测/绘图

        Args:
            data (KLine): 待转换的大周期K线数据
            if_replay (bool, optional): 是否用于数据回放（True时不做前向填充）. Defaults to False.

        Returns:
            pd.DataFrame: 转换后主周期长度的K线数据（含合约信息）

        ### 流程说明：
        1. 数据复制与时间列处理：复制原始数据，添加datetime_列用于后续处理
        2. 创建主周期时间框架：生成与主周期K线时间一致的空数据框（datas）
        3. 数据合并：通过datetime对齐大周期K线与主周期时间，填充NaN
        4. 数据清洗：排序、去重、前向填充（回放模式不填充）
        5. 补充合约信息：添加symbol、cycle等合约字段，返回完整K线数据
        """
        id = data.isresample  # 主周期ID
        data_ = data.pandas_object.copy()   # 复制原始K线数据
        data_['datetime_'] = data_.datetime.values  # 备份原始时间列
        cols = list(data_.columns)

        # 创建主周期时间框架（空数据，仅含主周期datetime）
        datetime = self._btklinedataset[id-1]["datetime"].values
        datas = pd.DataFrame(
            np.full((len(datetime), data_.shape[1]), np.nan), columns=cols
        )
        datas.datetime = datetime

        # 合并数据：通过datetime对齐大周期K线与主周期时间
        df = pd.merge(datas, data_, how='outer', on='datetime')

        # 填充合并后的NaN（优先用主周期数据，无则用大周期数据）
        for col in cols[1:]:
            df[col] = df.apply(
                lambda x: x[f'{col}_y'] if pd.isna(
                    x[f'{col}_x']) else x[f'{col}_x'],
                axis=1
            )

        # 数据清洗：排序、去重
        df = df[cols]
        df.sort_values(by=['datetime', 'open'],
                       na_position='first', ignore_index=True, inplace=True)
        df.drop_duplicates('datetime', keep='last',
                           ignore_index=True, inplace=True)

        # 确保数据长度与主周期一致
        if len(datetime) != df.shape[0]:
            df = df[~pd.isna(df.datetime)]
            df.reset_index(drop=True, inplace=True)

        # 数据填充：回放模式不填充（保留NaN），其他模式前向填充
        df = df[cols[:-1]] if if_replay else df[cols[:-1]].ffill()

        # 补充合约信息（确保与原始数据一致）
        df['symbol'] = data.symbol
        df['duration'] = data.cycle
        df['price_tick'] = data.price_tick
        df['volume_multiple'] = data.volume_multiple

        return df

    @classmethod
    def _resample(cls, cycle1: int, cycle2: int, data: pd.DataFrame, rule: str = "") -> tuple[list[int], pd.DataFrame]:
        """
        ## 周期重采样核心实现（低→高周期）
        ### 核心作用：
        - 将低周期K线数据（如300秒）重采样为高周期（如900秒），生成高周期OHLCV数据

        Args:
            cycle1 (int): 原始低周期（秒）
            cycle2 (int): 目标高周期（秒）
            data (pd.DataFrame): 原始低周期K线数据（含FILED.ALL字段）
            rule (str, optional): 时间规则（如'D'=日、'W'=周，用于日以上周期）. Defaults to "".

        Returns:
            tuple[list[int], pd.DataFrame]:
                - 第一个元素：重采样后数据在原始数据中的索引（plot_index）
                - 第二个元素：重采样后的高周期K线数据

        ### 核心逻辑：
        1. 计算重采样倍数（multi = cycle2 / cycle1）
        2. 时间对齐：找到第一个符合目标周期起点的K线（如900秒周期的0分0秒）
        3. 数据聚合：按倍数分组，聚合生成高周期OHLCV（open=首根开价，high=组内最高价等）
        4. 处理剩余数据：聚合最后一组不足倍数的K线
        """
        multi = int(cycle2 / cycle1)  # 重采样倍数（如300→900秒，multi=3）
        # 计算相邻K线的时间差（秒），用于检测周期异常
        time_diff = data.datetime.diff().apply(
            lambda x: x.seconds if not pd.isna(x) else 0).values
        datetime_series = data.datetime.values  # 原始时间序列
        start = datetime_series[0]  # 原始数据起始时间

        # 时间对齐：找到目标周期的第一个起点（如900秒周期的0分0秒）
        i = 0  # 第一个符合条件的K线索引
        max_search_idx = min(multi, len(datetime_series))  # 兜底：避免超出数组范围
        if "S" in rule:  # 秒级周期（如"300S"）
            for idx in range(max_search_idx):
                dt = datetime_series[idx]
                if pd.Timestamp(dt).second % cycle2 == 0:
                    i = idx
                    break
        elif "T" in rule:  # 分钟级周期（如"15T"）
            target_minute_interval = int(cycle2 / 60)  # 转为整数，避免浮点数取模精度问题
            for idx in range(max_search_idx):
                dt = datetime_series[idx]
                _dt = pd.Timestamp(dt)
                # 秒数为0，且分钟数是目标间隔的整数倍
                if _dt.second == 0 and (_dt.minute % target_minute_interval) == 0:
                    i = idx
                    break
        else:
            pass  # 其他周期（如小时/日）预留逻辑

        # 初始化结果容器
        array = data.values  # 原始K线数据（numpy数组）
        result = []
        plot_index: list[int] = []  # 重采样数据在原始数据中的索引

        # 处理第一个完整分组前的K线（不足multi根）
        if i > 0:  # 仅当i>0时，才存在前置不足分组的K线
            first_data = array[:i, :]  # 第一个分组前的K线
            # 聚合第一个分组前的K线（生成一根高周期K线）
            first_ohlcv = [
                start,  # 时间：取起始时间
                first_data[:, 1][0],  # open：首根开价
                first_data[:, 2].max(),  # high：组内最高价
                first_data[:, 3].min(),  # low：组内最低价
                first_data[:, 4][-1],  # close：最后一根收盘价
                first_data[:, 5].sum()  # volume：组内成交量总和
            ]
            result.append(first_ohlcv)
            plot_index.append(0)  # 记录索引：前置数据对应原始数据第0个索引
            # 更新剩余数据与时间差
            array = array[i:, :]
            time_diff = time_diff[i:] if len(time_diff) > i else []
        else:
            pass  # i=0时，无前置数据，直接处理后续完整分组

        #fr = True  # 是否从第一根K线开始重采样（修正后仅作为标记，避免重复添加）
        index = 0  # 当前分组起始索引
        array_len = len(array)  # 剩余数据长度

        # 按倍数分组，聚合高周期K线（修正：消除重复添加逻辑）
        for j in range(multi, array_len + 1):  # j到array_len，确保最后一个完整分组被处理
            # 分组条件：达到倍数或时间差异常（非原始周期）
            if (j % multi == 0) or (j < array_len and time_diff[j] != cycle1):
                length = j - index  # 当前分组长度
                if length <= 0:  # 跳过无效分组（长度为0或负数）
                    index = j
                    continue
                values = array[index:j, :]  # 当前分组数据
                # 聚合分组数据（仅添加一次，消除重复）
                group_ohlcv = [
                    values[:, 0][0],  # 时间：首根时间
                    values[:, 1][0],  # open：首根开价
                    values[:, 2].max(),  # high：组内最高价
                    values[:, 3].min(),  # low：组内最低价
                    values[:, 4][-1],  # close：最后一根收盘价
                    values[:, 5].sum()   # volume：组内成交量总和
                ]
                result.append(group_ohlcv)
                # 记录索引：对应原始数据的索引（需加上之前跳过的i）
                plot_index.append(index + i)
                # 更新索引
                index = j
                #fr = False  # 首次分组后，标记为非起始状态

        # 处理最后一组不足倍数的K线（修正：索引判断更严谨）
        if index < array_len:
            values = array[index:, :]
            if len(values) > 0:  # 仅当有剩余数据时才聚合
                last_ohlcv = [
                    values[:, 0][0],
                    values[:, 1][0],
                    values[:, 2].max(),
                    values[:, 3].min(),
                    values[:, 4][-1],
                    values[:, 5].sum()
                ]
                result.append(last_ohlcv)
                plot_index.append(index + i)  # 记录最后一组对应的原始索引

        # 确保plot_index与result长度一致（兜底处理）
        if len(plot_index) > len(result):
            plot_index = plot_index[:len(result)]
        elif len(plot_index) < len(result):
            # 补充缺失索引（若存在，取最后一个索引补全）
            last_idx = plot_index[-1] if plot_index else (len(data)-1)
            plot_index.extend([last_idx] * (len(result) - len(plot_index)))

        # 转换为DataFrame并返回
        resample_df = pd.DataFrame(result, columns=FILED.ALL)
        return plot_index, resample_df

    def resample(self, cycle: int, data: KLine = None, rule: str = None, **kwargs) -> KLine:
        """
        ## 对外暴露的K线周期转换接口（低→高周期）
        ### 核心作用：
        - 提供标准化接口，将指定K线数据转换为目标高周期，返回KLine实例

        Args:
            cycle (int): 目标高周期（秒），必须大于原始周期且为原始周期的倍数
            data (KLine, optional): 待转换的原始K线数据，默认使用主数据. Defaults to None.
            rule (str, optional): 时间规则（如'D'=日、'W'=周，用于日以上周期）. Defaults to None.

        Kwargs:
            online (bool): 是否在线获取多周期数据（True时从TQApi获取，默认True）

        Returns:
            KLine: 转换后的高周期K线数据实例

        ### 关键校验：
        1. 周期必须为整数
        2. 目标周期必须大于原始周期且为原始周期的倍数
        """
        # 优化模式下不执行转换，返回标记
        if self._isoptimize:
            return "BtOptimize"

        # 参数校验：周期必须为整数
        assert isinstance(cycle, int), "周期必须为整数"

        # 确定原始数据：默认使用主数据（_btklinedataset.default_kline）
        data = self._btklinedataset.default_kline if data is None else data
        main_cycle = data.cycle  # 原始主周期（秒）

        # 参数校验：目标周期必须大于原始周期且为倍数
        assert cycle > main_cycle and cycle % main_cycle == 0, '周期不能低于主周期并且为主周期的倍数'

        # 在线获取多周期数据（实盘或需要最新数据时）
        if self._api and kwargs.pop('online', True):
            # 从TQApi获取目标周期K线
            df = self._api.get_kline_serial(
                symbol=data.symbol, duration=cycle, data_length=len(data))
            # 时间格式转换与调整（确保与原始数据时间对齐）
            df.datetime = df.datetime.apply(time_to_datetime)
            timediff = timedelta(seconds=cycle)
            df.datetime = df.datetime.apply(lambda x: x + timediff)

            # 跳空处理（可选，消除非交易时间的价格跳变）
            if self._abc:
                df = abc(df, self._abc)
            else:
                if self._clear_gap:
                    # 计算相邻K线的时间差
                    time_delta = pd.Series(df.datetime.values).diff().bfill()
                    # 正常周期时间差（含10:15-10:30停盘时间）
                    cycle_ls = [timedelta(seconds=cycle),
                                timedelta(seconds=900 + cycle)]
                    # 识别跳空K线索引
                    _gap_index = ~time_delta.isin(cycle_ls)
                    _gap_index = np.argwhere(_gap_index.values).flatten()
                    _gap_index = np.array(
                        list(filter(lambda x: x > 0, _gap_index)))

                    # 消除跳空：调整跳空后的价格
                    if _gap_index.size > 0:
                        _gap_diff = df.open.values[_gap_index] - \
                            df.close.values[_gap_index - 1]
                        for id, ix in enumerate(_gap_index):
                            df.loc[ix:, FILED.OHLC] = df.loc[ix:, FILED.OHLC].apply(
                                lambda x: x - _gap_diff[id])

            # 时间过滤：仅保留原始数据时间范围内的K线
            df = df[df.datetime >= (data.datetime.iloc[0])]
            df.reset_index(drop=True, inplace=True)
            rdata = df  # 重采样后的数据

        # 本地重采样（回测模式，使用原始数据计算）
        else:
            # 选择原始数据（跟随主数据或使用K线原始数据）
            df = data.pandas_object if data.follow else data.kline_object
            # 生成时间规则字符串（如300秒→"300S"，900秒→"15T"）
            cycle_string = rule if (isinstance(rule, str) and rule in ['D', 'W', 'M']) else \
                f"{cycle}S" if cycle < 60 else (
                    f"{int(cycle/60)}T" if cycle < 3600 else f"{int(cycle/3600)}H"
            )
            # 调用核心重采样逻辑
            plot_index, rdata = self._resample(
                main_cycle, cycle, df[FILED.ALL], cycle_string)

        # 生成新的指标ID（关联主数据ID，标记为高周期数据）
        _id = self._btklinedataset.num
        id = data.id.copy(plot_id=_id, data_id=_id, resample_id=data.data_id)

        # 补充合约信息（目标周期的合约参数）
        symbolinfo_dict = data.symbol_info.filt_values(duration=cycle)
        rdata.add_info(**symbolinfo_dict)

        # 配置参数：传递转换数据、绘图索引等
        kwargs.update(
            dict(
                conversion_object=data.pandas_object if data.follow else data.kline_object,
                plot_index=plot_index,
                source_object=data,
                source_index=data.data_id
            )
        )

        # 创建并返回高周期KLine实例（标记为isresample=True）
        return KLine(rdata, id=id, isresample=True, name=f"datas{_id}", **kwargs)

    def __rolling_window(self, v: np.ndarray, window: int = 1, if_index=False) -> np.ndarray:
        """
        ## 生成滚动窗口数据的工具方法
        ### 核心作用：
        - 将1D/2D数组转换为滚动窗口格式（如窗口大小3，数组长度5→输出3个窗口），用于时序特征提取

        Args:
            v (np.ndarray): 输入数组（1D或2D）
            window (int, optional): 窗口大小，默认1（无滚动）. Defaults to 1.
            if_index (bool, optional): 是否在窗口中包含原始索引. Defaults to False.

        Returns:
            np.ndarray: 滚动窗口数据（shape=(窗口数, window, 特征数)）

        ### 核心逻辑：
        1. 索引处理：若if_index=True，在数组前添加索引列
        2. 滚动窗口计算：使用numpy stride_tricks生成滚动窗口（高效无复制）
        3. 不足窗口长度处理：窗口大小>1时，前window-1个窗口补NaN
        """
        # 若需要包含索引，在数组前添加索引列（shape=(len(v), 1)）
        if if_index:
            v = np.column_stack((np.arange(len(v)), v))

        dim0, dim1 = v.shape  # 输入数组维度（dim0=时间步，dim1=特征数）
        stride0, stride1 = v.strides  # 数组 strides（内存步长）

        # 处理窗口大小>1的情况：前window-1个窗口补NaN
        redata = []
        if window > 1:
            for i in range(window - 1):
                d = v[:i + 1, :]  # 前i+1个元素
                nad = np.full((window - d.shape[0], dim1), np.nan)  # 补NaN
                redata.append(np.vstack((nad, d)))  # 拼接NaN与有效数据

        # 生成滚动窗口（使用stride_tricks，避免数据复制）
        data = as_strided(
            v,
            # 输出shape：(窗口数, 窗口大小, 特征数)
            shape=(dim0 - (window - 1), window, dim1),
            strides=(stride0, stride0, stride1)  # 步长：沿时间轴1步，窗口内1步，特征轴1步
        )

        # 拼接补NaN的窗口与正常窗口，返回最终结果
        return np.vstack((np.array(redata), data)) if window > 1 else data

    @classmethod
    def _replay(cls, cycle1: int, cycle2: int, data: pd.DataFrame, rule: str = "") -> pd.DataFrame:
        """
        ## 数据回放核心实现（高→低周期）
        ### 核心作用：
        - 将高周期K线数据（如900秒）拆分为低周期回放数据（如300秒），模拟实时行情逐步推送

        Args:
            cycle1 (int): 目标低周期（秒）
            cycle2 (int): 原始高周期（秒）
            data (pd.DataFrame): 原始高周期K线数据（含FILED.ALL字段）
            rule (str, optional): 时间规则（如'D'=日、'W'=周）. Defaults to "".

        Returns:
            pd.DataFrame: 回放后的低周期K线数据

        ### 核心逻辑：
        1. 计算回放倍数（multi = cycle2 / cycle1）
        2. 时间对齐：找到第一个符合目标低周期起点的K线
        3. 数据拆分：将每根高周期K线拆分为multi根低周期K线，实时更新OHLCV（如high取累计最高价）
        4. 处理剩余数据：拆分最后一根高周期K线
        """
        multi = int(cycle2 / cycle1)  # 回放倍数（如900→300秒，multi=3）
        # 计算相邻K线的时间差（秒）
        time_diff = data.datetime.diff().apply(lambda x: x.seconds).values
        datetime = data.datetime.values  # 原始时间序列

        # 时间对齐：找到目标低周期的第一个起点
        i = 0
        if "S" in rule:
            for i, dt in enumerate(datetime[:multi]):
                if pd.Timestamp(dt).second % cycle2 == 0:
                    break
        elif "T" in rule:
            for i, dt in enumerate(datetime[:multi]):
                _dt = pd.Timestamp(dt)
                if _dt.second == 0 and _dt.minute % (cycle2 / 60) == 0:
                    break
        else:
            ...

        # 初始化结果容器
        array = data.values  # 原始高周期数据（numpy数组）
        result = []

        # 处理第一个完整分组前的K线（拆分为低周期）
        if i:
            first_data = array[:i, :]  # 第一个分组前的高周期K线
            for ix in range(i):
                # 提取当前低周期K线数据
                dtime, _open, _high, _low, close, _volumn = first_data[ix]
                # 累计更新OHLCV（模拟实时行情）
                if ix:
                    high = max(high, _high)  # 累计最高价
                    low = min(low, _low)      # 累计最低价
                    volumn += _volumn         # 累计成交量
                else:
                    open, high, low, volumn = _open, _high, _low, _volumn
                # 添加到回放结果
                result.append([dtime, open, high, low, close, volumn])
            # 更新剩余数据与时间差
            array = array[i:, :]
            time_diff = time_diff[i:]

        # 拆分高周期K线为低周期回放数据
        for j, row in enumerate(array):
            dtime, _open, _high, _low, close, _volumn = row
            # 分组条件：新的高周期K线或时间差异常
            if j % multi == 0 or time_diff[j] != cycle1:
                # 初始化新分组的OHLCV
                open, high, low, volumn = _open, _high, _low, _volumn
            else:
                # 累计更新当前分组的OHLCV
                high = max(high, _high)
                low = min(low, _low)
                volumn += _volumn
            # 添加到回放结果
            result.append([dtime, open, high, low, close, volumn])

        # 转换为DataFrame并返回
        return pd.DataFrame(result, columns=FILED.ALL)

    def __multi_data_replay(self, data: KLine) -> tuple[list[str], pd.DataFrame]:
        """
        ## 多周期数据回放处理方法
        ### 核心作用：
        - 将高周期K线数据回放为低周期，对齐主周期时间，生成回放数据与索引

        Args:
            data (KLine): 待回放的高周期K线数据

        Returns:
            tuple[list[str], pd.DataFrame]:
                - 第一个元素：回放数据的时间列表（预留，当前未使用）
                - 第二个元素：回放后的低周期K线数据
        """
        # 第一步：转换高周期数据为主周期时间框架（不填充）
        rdata = self.__multi_data_resample(data, True)
        datetime = rdata.datetime.values  # 主周期时间序列
        rdata = rdata[FILED.OHLCV].values  # 提取OHLCV数据

        # 第二步：生成滚动窗口数据（用于逐周期更新回放数据）
        # 主数据滚动窗口（含索引），高周期数据滚动窗口
        rolling_data = zip(
            self.__rolling_window(
                self._datas[data.id[0]][0].values, if_index=True),
            self.__rolling_window(rdata)
        )

        if_first = True  # 是否为第一根K线
        index_multi_cycle = []  # 多周期索引（标记高周期切换）
        _index = 0  # 高周期数据索引

        # 第三步：逐周期更新回放数据
        for d, rd in rolling_data:
            d, rd = d[0], rd[0]  # 取当前周期窗口数据
            i = d[0]  # 当前主周期索引

            # 处理高周期数据NaN（填充逻辑）
            if np.isnan(rd).any():
                if if_first:
                    # 第一根K线：用主数据填充
                    first_d = d[2:]  # 主数据的OHLCV
                    rdata[i, :] = first_d
                    if_first = False
                else:
                    # 非第一根K线：累计更新OHLCV
                    _, lasthigh, lastlow, lastclose, lastvolume = d[2:]
                    pre_open, pre_high, pre_low, _, pre_volume = first_d
                    # 累计计算：开价不变，高低价取累计极值，成交量累加
                    first_d = [
                        pre_open,
                        max(lasthigh, pre_high),
                        min(lastlow, pre_low),
                        lastclose,
                        lastvolume + pre_volume
                    ]
                    rdata[i, :] = first_d
            else:
                # 高周期数据有效：更新索引，标记新的高周期
                _index += 1
                if_first = True
            # 记录多周期索引
            index_multi_cycle.append(_index)

        # 第四步：整理回放数据（添加时间列）
        rdata = pd.DataFrame(rdata, columns=FILED.OHLCV)
        rdata.insert(0, 'datetime', datetime)

        return rdata, index_multi_cycle

    def replay(self, cycle: int, data: KLine = None, rule: str = None, **kwargs) -> KLine:
        """
        ## 对外暴露的数据回放接口（高→低周期）
        ### 核心作用：
        - 将高周期K线数据回放为低周期，模拟实时行情，返回KLine实例（实盘模式不生效）

        Args:
            cycle (int): 目标低周期（秒），必须大于原始周期且为原始周期的倍数
            data (KLine, optional): 待回放的高周期K线数据，默认使用主数据. Defaults to None.
            rule (str, optional): 时间规则（如'D'=日、'W'=周）. Defaults to None.

        Returns:
            KLine: 回放后的低周期K线数据实例

        ### 关键校验：
        1. 周期必须为整数
        2. 目标周期必须大于原始周期且为原始周期的倍数
        """
        # 参数校验：周期必须为整数
        assert isinstance(cycle, int), "周期必须为整数"

        # 确定原始数据：默认使用主数据
        data = self._btklinedataset.default_kline if data is None else data
        assert isinstance(data, KLine), "data必须为KLine类型"
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
        rdata = self._replay(main_cycle, cycle, df[FILED.ALL], cycle_string)

        # 生成新的指标ID（关联主数据ID，标记为回放数据）
        _id = self._btklinedataset.num
        id = data.id.copy(plot_id=_id, data_id=_id, replay_id=data.data_id)

        # 补充合约信息（目标周期的合约参数）
        symbolinfo_dict = data.symbol_info.filt_values(duration=cycle)
        rdata.add_info(**symbolinfo_dict)

        # 生成重采样数据（用于回放时的时间对齐）
        plot_index, resample_data = self._resample(
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

    def _update_replay_datas(self) -> tuple:
        """
        ## 实盘模式下更新图表数据的方法
        ### 核心作用：
        - 从TQApi获取最新K线与指标数据，处理HA/K线转换，整理为绘图所需格式

        Args:
            length (int, optional): 图表显示的数据长度（默认显示最近10根K线）. Defaults to 10.

        Returns:
            tuple:
                - 第一个元素：绘图数据源列表（每个元素对应一个K线数据的绘图配置）
                - 第二个元素：持仓状态列表（每个元素含持仓方向与开仓价）
        """
        length = self.update_length
        source = []
        _btind_span = []
        index = self._btindex  # 全局回放索引（随K线推进递增）
        sid = self.sid
        for i, kline in enumerate(self._btklinedataset.get_replay_data(index).values()):
            tq_data = kline.iloc[-length:]
            _datetime = tq_data.datetime.values
            volume_data = kline.volume[-20:]
            volume5 = volume_data.rolling(
                5).mean().values[-length:]
            volume10 = volume_data.rolling(
                10).mean().values[-length:]
            lh = tq_data[['low', 'high']]
            # 获取当前持仓状态
            result = self.account._get_history_result(i, index)
            pos = result[1]
            price = self._btklinedataset[i]._broker._cost_price
            _btind_span.append([pos, price])
            # 处理时间与成交量数据（取最近length根）
            volume = tq_data.volume.values
            # 处理HA布林带K线转换（若启用）
            tq_data = self._to_ha(tq_data, self._btklinedataset.isha[i])
            # 处理线性回归K线转换（若启用）
            tq_data = self._to_lr(tq_data, self._btklinedataset.islr[i])
            # 提取OHLC数据（取最近length根）
            open = tq_data.open.values
            high = tq_data.high.values
            low = tq_data.low.values
            close = tq_data.close.values
            # 整理K线基础绘图数据（含涨跌标记inc）
            Low = lh.min(1).values
            High = lh.max(1).values
            data = dict(
                index=kline.index.values[-length:],
                datetime=_datetime[-length:],
                open=open[-length:],
                high=high[-length:],
                low=low[-length:],
                close=close[-length:],
                volume=volume[-length:],
                volume5=volume5[-length:],
                volume10=volume10[-length:],
                inc=(tq_data.close >= tq_data.open).values.astype(
                    np.uint8).astype(str)[-length:],
                Low=Low[-length:],  # 预留字段（用于指标显示）
                High=High[-length:]  # 预留字段（用于指标显示）
            )
            # 处理指标绘图数据
            ind_record = self._indicator_record[i]  # 指标记录（含显示配置）
            for isplot, ind_name, lines, rlines, doubles, plotinfo in ind_record:
                lineinfo = plotinfo.get('linestyle', {})  # 线型配置
                overlap = plotinfo.get("overlap")
                signal_info: dict = plotinfo.get('signalstyle', {})
                # 获取完整指标数据
                ind_key = ind_name[0] if doubles else ind_name
                full_ind_data = self._btindicatordataset[ind_key].pandas_object.iloc[:index+1]

                if signal_info:
                    for k, v in signal_info.items():
                        (signalkey, signalcolor, signalmarker,
                         signaloverlap, signalshow, signalsize, signallabel) = list(v.values())

                        if not signalshow:
                            continue
                        key = f"{ind_key}{sid}_{k}"
                        if signaloverlap:
                            price = kline[signalkey].values
                        else:
                            try:
                                # 用重置索引的指标
                                price = full_ind_data[signalkey].values
                            except:
                                price = full_ind_data[k].values

                        svalues = full_ind_data[k].values  # 信号值（重置索引后）
                        signal_mask = svalues > 0  # 信号触发的掩码
                        sprice = price[signal_mask]  # 信号价格
                        # 信号时间（与K线严格对齐）
                        sdatetime = kline.datetime.values[signal_mask]
                        # 修复：添加 index 字段，确保信号索引与K线索引对齐
                        sindex = kline.index.values[signal_mask]
                        signal_data = dict(
                            datetime=sdatetime[-length:],
                            price=sprice[-length:],
                            size=[signalsize,]*length,
                            index=sindex[-length:],  # 添加索引字段
                        )

                        if signallabel:
                            signal_data.update(dict(text=[signallabel and signallabel["text"]
                                                          or "text",],))
                        data.update({key: signal_data})

                # ========== 核心修复4：后续指标处理改用重置索引后的数据 ==========
                # 基于全局索引截取递增指标窗口
                ind = full_ind_data.iloc[-length:].values

                # 处理多指标合并场景（doubles标记）
                if doubles:
                    ind_name = ind_name[0]
                    ind = ind[:, doubles]  # 重新排序
                    # 整理显示配置与指标列名
                    len_ind = len(lines[1])
                    _isplot = list(
                        reduce(lambda x, y: x + y, isplot))  # 合并显示开关
                    _lines = list(reduce(lambda x, y: x + y, lines))    # 合并列名
                    lencol = len(lines[0])
                    # 按显示开关添加指标数据
                    for ix in range(len(doubles)):
                        if _isplot[ix]:
                            data.update(
                                {_lines[ix]: ind[:, ix].tolist()[-length:]})
                    data.update(self.__get_ind_HL(
                        ind_name, ind[:, lencol:], length))
                    # 前向填充NaN（确保数据完整性）
                    ind = ffillnan(ind[:, -len_ind:])
                else:
                    # 处理单指标场景
                    if any(isplot):
                        if len(isplot) == 1:
                            # 单列指标：直接添加数据
                            value = ind.tolist()[-length:]
                            data.update({lines[0]: value})
                            # 处理柱状图指标（line_dash='vbar'）
                            if lineinfo and rlines[0] in lineinfo and lineinfo[rlines[0]].get('line_dash', None) == 'vbar':
                                # 添加涨跌标记（用于柱状图颜色区分）
                                data.update({f"{rlines[0]}_inc": list(
                                    map(lambda x: "1" if x > 0. else "0", value))})
                                # 添加零线（用于柱状图基准）
                                data.update({'zeros': [0.,] * length})
                            if not overlap:
                                data.update(self.__get_ind_HL(
                                    ind_name, ind, length))
                        else:
                            ils = []
                            # 多列指标：按列添加数据
                            for ix, (_name, ov) in enumerate(zip(lines, overlap.values())):
                                if isplot[ix]:
                                    value = ind[:, ix].tolist()[-length:]
                                    data.update({_name: value})
                                    # 处理柱状图指标
                                    if lineinfo and rlines[ix] in lineinfo and lineinfo[rlines[ix]].get('line_dash', None) == 'vbar':
                                        data.update({f"{rlines[ix]}_inc": list(
                                            map(lambda x: "1" if x > 0. else "0", value))})
                                        if 'zeros' not in data:
                                            data.update(
                                                {'zeros': [0.,] * length})
                                    if not ov:
                                        ils.append(ix)
                            if ils:
                                data.update(self.__get_ind_HL(
                                    ind_name, ind[:, ils], length))
                    # 前向填充NaN
                    # if ind_name != "stop_lines":
                    # ind = ffillnan(ind)

            # 添加当前K线的绘图数据到列表
            source.append(data)

        # self._get_account_info()

        return [sid, source], [sid, _btind_span], self._account.account_info[index]

    def _update_live_datas(self, isswitch: bool = False) -> tuple:
        """
        ## 弃用
        ## 实盘模式下更新图表数据的方法
        ### 核心作用：
        - 从TQApi获取最新K线与指标数据，处理HA/K线转换，整理为绘图所需格式

        Args:
            length (int, optional): 图表显示的数据长度（默认显示最近10根K线）. Defaults to 10.

        Returns:
            tuple:
                - 第一个元素：绘图数据源列表（每个元素对应一个K线数据的绘图配置）
                - 第二个元素：持仓状态列表（每个元素含持仓方向与开仓价）
        """
        length = 100
        sid = self.sid
        _sid, _cid = loadData(self._id_dir)
        if sid != _sid:
            return [sid, [{} for _ in range(self._btklinedataset.num)]], [sid, [[0., 0.] for _ in range(self._btklinedataset.num)]], self._get_account_info()
        source = []  # 绘图数据源列表
        _btind_span = []  # 持仓状态列表（用于显示持仓线）
        # 遍历所有K线数据，生成绘图数据
        for i, kline in enumerate(self._btklinedataset.values()):
            if _cid != i:
                # 当前策略非当前K线跳过
                source.append({})
                _btind_span.append([0., 0.])
                continue
            # 获取TQ实盘数据
            # kline._dataset.pandas_object
            tq_data = kline.pandas_object.iloc[-length:]
            volume = tq_data.volume.iloc[-length:]
            volume5 = volume.rolling(5).mean().values
            volume10 = volume.rolling(10).mean().values
            _datetime = tq_data.datetime.values
            # 获取当前持仓状态
            position = kline.position
            pos = position.pos
            # 记录持仓方向与开仓价（多头取open_price_long，空头取open_price_short）
            price = (position.open_cost_long if pos >
                     0 else position.open_cost_short) if pos else 0.
            _btind_span.append([pos, price])
            # 处理时间与成交量数据（取最近length根）
            volume = tq_data.volume.values
            # 处理HA布林带K线转换（若启用）
            tq_data = self._to_ha(tq_data, self._btklinedataset.isha[i])
            # 处理线性回归K线转换（若启用）
            tq_data = self._to_lr(tq_data, self._btklinedataset.islr[i])
            # 提取OHLC数据（取最近length根）
            open = tq_data.open.values
            high = tq_data.high.values
            low = tq_data.low.values
            close = tq_data.close.values
            # 整理K线基础绘图数据（含涨跌标记inc）
            lh = tq_data[['low', 'high']]
            Low = lh.min(1).values
            High = lh.max(1).values
            data = dict(
                index=tq_data.index.values,
                datetime=_datetime,
                open=open,
                high=high,
                low=low,
                close=close,
                volume=volume,
                volume5=volume5,
                volume10=volume10,
                inc=(tq_data.close >= tq_data.open).values.astype(
                    np.uint8).astype(str),
                Low=Low,  # 预留字段（用于指标显示）
                High=High,  # 预留字段（用于指标显示）
            )
            # 处理指标绘图数据
            ind_record = self._indicator_record[i]  # 指标记录（含显示配置）
            for isplot, ind_name, lines, rlines, doubles, plotinfo in ind_record:
                lineinfo = plotinfo.get('linestyle', {})  # 线型配置
                overlap = plotinfo.get("overlap")
                signal_info: dict = plotinfo.get('signalstyle', {})
                # 获取指标数据
                ind_key = doubles and ind_name[0] or ind_name
                ind = self._btindicatordataset[ind_key].pandas_object
                # ========== 核心修复1：重置指标索引为相对索引（0~length-1） ==========
                if signal_info:
                    for k, v in signal_info.items():
                        signalkey, signalcolor, signalmarker, signaloverlap, signalshow, signalsize, signallabel = list(
                            v.values())

                        if signalshow:
                            # ma0_long_signal(标识)
                            key = f"{ind_key}{sid}_{k}"
                            if signaloverlap:
                                price = kline.pandas_object[signalkey].values
                            else:
                                try:
                                    # 用重置索引的指标
                                    price = ind[signalkey].values
                                except:
                                    price = ind[k].values

                            # ========== 核心修复2：基于相对索引筛选信号 ==========
                            # 信号值（重置索引后）
                            svalues = ind[k].values
                            # issignal = svalues[-1] > 0  # 当前值有交易信号
                            signal_mask = svalues > 0  # 信号触发的掩码
                            sprice = price[signal_mask]  # 信号价格
                            sindex = kline.pandas_object.index.values[signal_mask]
                            # 信号时间（与K线严格对齐）
                            sdatetime = kline.pandas_object['datetime'].values[signal_mask]
                            slen = len(sprice)

                            # ========== 核心修复3：信号index改为相对索引 ==========
                            signal_data = dict(
                                index=sindex,
                                datetime=sdatetime,
                                price=sprice,
                                size=[signalsize,]*slen,
                            )
                            if signallabel:
                                signal_data.update(dict(text=[signallabel and signallabel["text"]
                                                              or "text",]*slen,))
                            data.update({key: signal_data})

                # ========== 核心修复4：后续指标处理改用重置索引后的数据 ==========
                ind = ind.iloc[-length:].values

                # 处理多指标合并场景（doubles标记）
                if doubles:
                    ind_name = ind_name[0]
                    ind = ind[:, doubles]  # 重新排序
                    # 整理显示配置与指标列名
                    len_ind = len(lines[1])
                    _isplot = list(
                        reduce(lambda x, y: x + y, isplot))  # 合并显示开关
                    _lines = list(reduce(lambda x, y: x + y, lines))    # 合并列名
                    lencol = len(lines[0])
                    # 按显示开关添加指标数据
                    for ix in range(len(doubles)):
                        if _isplot[ix]:
                            data.update(
                                {_lines[ix]: ind[:, ix].tolist()[-length:]})
                    data.update(self.__get_ind_HL(
                        ind_name, ind[:, lencol:], length))
                    # 前向填充NaN（确保数据完整性）
                    ind = ffillnan(ind[:, -len_ind:])
                else:
                    # 处理单指标场景
                    if any(isplot):
                        if len(isplot) == 1:
                            # 单列指标：直接添加数据
                            value = ind.tolist()[-length:]
                            data.update({lines[0]: value})
                            # 处理柱状图指标（line_dash='vbar'）
                            if lineinfo and rlines[0] in lineinfo and lineinfo[rlines[0]].get('line_dash', None) == 'vbar':
                                # 添加涨跌标记（用于柱状图颜色区分）
                                data.update({f"{rlines[0]}_inc": list(
                                    map(lambda x: "1" if x > 0. else "0", value))})
                                # 添加零线（用于柱状图基准）
                                data.update({'zeros': [0.,] * length})
                            if not overlap:
                                data.update(self.__get_ind_HL(
                                    ind_name, ind, length))
                        else:
                            ils = []
                            # 多列指标：按列添加数据
                            for ix, (_name, ov) in enumerate(zip(lines, overlap.values())):
                                if isplot[ix]:
                                    value = ind[:, ix].tolist()[-length:]
                                    data.update({_name: value})
                                    # 处理柱状图指标
                                    if lineinfo and rlines[ix] in lineinfo and lineinfo[rlines[ix]].get('line_dash', None) == 'vbar':
                                        data.update({f"{rlines[ix]}_inc": list(
                                            map(lambda x: "1" if x > 0. else "0", value))})
                                        if 'zeros' not in data:
                                            data.update(
                                                {'zeros': [0.,] * length})
                                    if not ov:
                                        ils.append(ix)
                            if ils:
                                data.update(self.__get_ind_HL(
                                    ind_name, ind[:, ils], length))
                    ind = ffillnan(ind)

            # 添加当前K线的绘图数据到列表
            source.append(data)
        return [sid, source], [sid, _btind_span], self._get_account_info()

    def wait_update(self, deadline=60) -> bool:
        return self.api.wait_update(deadline=deadline)

    def __get_ind_HL(self, ind_name: str, ind: np.ndarray, length=None):
        if length is None:
            if len(ind.shape) > 1:
                max_value, min_value = np.max(ind, axis=1).tolist(
                ), np.min(ind, axis=1).tolist()
            else:
                max_value = min_value = ind.tolist()
            return {f"{ind_name}_h": max_value, f"{ind_name}_l": min_value}
        else:
            if len(ind.shape) > 1:
                max_value, min_value = np.max(ind, axis=1).tolist(
                )[-length:], np.min(ind, axis=1).tolist()[-length:]
            else:
                max_value = min_value = ind.tolist()[-length:]
            return {f"{ind_name}_h": max_value, f"{ind_name}_l": min_value}

    def _get_account_info(self) -> str:
        """
        ## 获取账户信息字符串（用于实盘日志/控制台输出）
        ### 核心作用：
        - 整合账户关键财务指标，生成易读的字符串

        Returns:
            str: 账户信息字符串（含权益、可用资金、盈亏、保证金等）
        """
        return " ".join([
            f"账户权益:{self.account.balance:.2f} ",
            f"可用资金:{self.account.available:.2f} ",
            f"浮动盈亏:{self.account.float_profit:.2f} ",
            f"持仓盈亏:{self.account.position_profit:.2f} ",
            f"本交易日内平仓盈亏:{self.account.close_profit:.2f} ",
            f"保证金占用:{self.account.margin:.2f} ",
            f"手续费:{self.account.commission:.2f} ",
            f"风险度:{self.account.risk_ratio:.2f} "
        ])

    def btind_like(self, ds: IndSeries | IndFrame | tuple[int] | int, **kwargs) -> IndSeries | IndFrame:
        """
        ## 创建自定义指标的工具方法（初始化全NaN数据）
        ### 核心作用：
        - 根据参考数据或维度，生成结构一致的全NaN指标（IndSeries/IndFrame），供用户后续赋值

        ### 适用场景：
        - 手动计算自定义指标（如动态止损价、自定义信号）
        - 确保自定义指标与参考数据（如K线、其他指标）结构一致

        Args:
            ds (Union[IndSeries, IndFrame, tuple[int], int]): 参考数据或维度：
                - IndSeries/IndFrame：生成与参考指标结构（长度、列数）一致的指标
                - tuple[int]：维度元组（如(100, 2)表示100行2列）
                - int：长度（生成1列、指定长度的IndSeries）

        ### Kwargs:
            指标属性配置（如lines=列名、category=指标分类、isplot=是否绘图等，同IndSetting）

        Returns:
            Union[IndSeries, IndFrame]: 全NaN的自定义指标（IndSeries对应1列，IndFrame对应多列）

        ### 示例：
        >>> # 1. 参考MA指标生成自定义止损价指标（同长度）
        >>> self.ma5 = self.data.close.ema(5)
        >>> self.stop_price = self.btind_like(self.ma5, name='stop_price', isplot=True)
        >>> # 2. 手动赋值止损价（如MA5*0.98）
        >>> self.stop_price[:] = self.ma5 * 0.98
        >>> 
        >>> # 3. 按维度生成2列、100行的自定义指标
        >>> self.custom_ind = self.btind_like((100, 2), lines=['ind1', 'ind2'], category='momentum')
        """
        # 处理维度输入：tuple[int]（如(100,2)）
        if isinstance(ds, tuple):
            assert all([isinstance(i, int) and i >
                       0 for i in ds]), "数组ds元素必须为正整数"
        # 处理长度输入：int（如100→1列100行）
        elif isinstance(ds, int):
            assert ds > 0, "ds必须为正整数"
            ds = (ds,)
        # 处理参考指标输入：IndSeries/IndFrame
        else:
            # 继承参考指标的配置（如ID、分类、绘图开关）
            kwargs = {**ds.ind_setting, **kwargs}
            ds = ds.shape  # 提取参考指标的维度（长度/行列数）

        # 初始化默认配置（基于IndSetting）
        default_kwargs = IndSetting(0, 0, 'btind', ['btind',])
        for k, v in default_kwargs.items():
            if k not in kwargs:
                kwargs.update({k: v})

        # 生成1列指标（IndSeries）
        if len(ds) == 1:
            # 确保lines参数存在（列名）
            kwargs.update(dict(lines=[kwargs.get('name', 'custom_line'),]))
            return IndSeries(ds, **kwargs)
        # 生成多列指标（IndFrame）
        else:
            # 校验列名数量与维度一致
            assert len(
                kwargs['lines']) == ds[1], f"维度{ds}与列名{kwargs['lines']}不一致，请设置lines"
            return IndFrame(ds, **kwargs)

    def __setattr__(self, name, value: KLine | IndSeries | IndFrame | Line):
        """
        ## 重写属性设置方法，用于策略初始化时特殊处理指标和K线数据

        - 当策略未初始化时，会对特定类型的属性（如指标、K线数据）进行额外处理，
        - 包括注册到对应的数据集中、设置名称关联等，之后再调用父类的属性设置方法
        """
        # 策略初始化时生效
        if not self._in_next_context:
            if self._first_start:
                # 参数优化和实盘时，当运行self.get_kline时返回内置数据，不需要重新获取数据
                if self._isoptimize or self._is_live_trading:
                    if name in self._btklinedataset:
                        return self._btklinedataset[name]

            value_type = type(value)
            # 收录内置指标：若值为指标类型且长度匹配，则添加到指标数据集
            if value_type in BtIndType and len(value) in self._btklinedataset.lengths:
                value.sname = name  # 设置指标名称
                # 修复：确保指标对象的 strategy_id 与当前策略一致
                # 否则指标对象的 strategy_instance 会错误地指向其他策略实例
                if value.id.strategy_id != self._sid:
                    value._indsetting.id = value._indsetting.id.copy(strategy_id=self._sid)
                    # 同步更新所有子行(Line)的 strategy_id
                    for line_name in value.lines:
                        line = getattr(value, line_name, None)
                        if line is not None and hasattr(line, '_indsetting'):
                            line._indsetting.id = line._indsetting.id.copy(strategy_id=self._sid)
                # 处理上采样关联
                if value._upsample_name:
                    for k, v in self._btindicatordataset.items():
                        if v._dataset.upsample_object is not None:
                            if value.equals(v._dataset.upsample_object):
                                v._upsample_name = name
                                value._upsample_name = v.sname
                self._btindicatordataset.add_data(name, value)
            # 收录K线数据：若值为K线数据类型，则添加到K线数据集
            elif value_type in KLineType:
                value.sname = name  # 设置K线数据名称
                # 检查KLine的id是否正确：当strategy_id不匹配或plot_id/data_id为默认值0时，重新分配ID
                # 修复：原条件 value.id.plot_id!=len(self._btklinedataset) 在 len=0 时
                # 与 plot_id=0 相等导致第一个KLine永远不被更新，改为直接检查 strategy_id
                need_update_id = value.id.strategy_id != self._sid or (value.id.plot_id==0 and value.id.data_id==0)
                if need_update_id:
                    from ..utils import BtID
                    id = len(self._btklinedataset)
                    btid = BtID(self._sid, id, id)
                    value._indsetting.id = btid
                    value.name=name
                    # 问题的核心就是 ： KLine 对象本身的 id 被正确更新了，但它内部的 Line 对象（如 close 、 low 、 high 、 open 、 volume 等）的 id 没有同步更新。
                    # 让我用更直观的方式解释一下：
                    ### 问题场景
                    # 当策略2（CMOStrategy）初始化时：

                    # 1. self.data = LocalDatas.pp2601_60.kline 创建了一个 KLine 对象，其 id.strategy_id = 0 （默认值）
                    # 2. 这个 KLine 对象内部会为每一列（open、high、low、close、volume）创建对应的 Line 对象，这些 Line 对象的 id.strategy_id 也是默认值 0
                    # 3. Strategy.__setattr__ 检测到 KLine 的 id.strategy_id 不正确，将其更新为 1
                    # 4. 但是！ Line 对象的 id 没有同步更新，仍然是 0
                    ### 影响
                    # 当 Stop 实例访问 self.kline.close[-1] 时：

                    # - self.kline 返回的 KLine 对象的 id.strategy_id = 1 ✅
                    # - self.kline.close 返回的 Line 对象的 id.strategy_id = 0 ❌
                    # - Line.strategy_instance 使用 id.strategy_id = 0 查找策略实例，返回策略1而不是策略2
                    # - btindex 返回策略1的回测索引，而不是策略2的
                    # - 结果： close[-1] 返回策略1的数据，而不是策略2的数据
                    ### 修复方案
                    # 同步更新所有子行(Line)的ID，确保索引访问时能正确关联到策略实例
                    for line_name in value.lines:
                        line = getattr(value, line_name, None)
                        if line is not None and hasattr(line, '_indsetting'):
                            line._indsetting.id = btid.copy()
                # 处理多次赋值的情况，保持指标ID一致性
                if name in self._btklinedataset:  # 多次赋值，即替换为最新数据
                    if hasattr(self._account, "brokers"):
                        self._account.brokers.pop(
                            list(self._btklinedataset.keys()).index(name))
                    btid = self._btklinedataset[name]._indsetting.id.copy()
                    for v in [*value.line, value]:
                        v._indsetting.id = value._indsetting.id.copy(
                            strategy_id=btid.strategy_id,
                            plot_id=btid.plot_id,
                            data_id=btid.data_id,
                        )

                self._btklinedataset.add_data(name, value)
                value._set_broker()

        # 调用父类的属性设置方法
        return super().__setattr__(name, value)

    def __getattribute__(self, name) -> IndFrame | IndSeries | Line | KLine | Any:
        """## 重写属性获取方法，直接调用父类的属性获取逻辑"""
        return super().__getattribute__(name)

    # ------------------------------
    # 强化学习（RL）相关方法
    # ------------------------------
    def get_signal_features(self) -> np.ndarray | None:
        """
        ## 获取用于强化学习的信号特征

        - 当启用强化学习（rl=True）时，通过__process_quant_features处理特征，
        - 返回处理后的特征数组（numpy格式），否则返回None
        """
        if self.rl:
            return self.__process_quant_features()

    def set_process_quant_features(
        self,
        # 归一化方法：'standard'/'robust'/'minmax'/'rolling'
        normalize_method: Literal['standard',
                                  'robust', 'minmax', 'rolling'] = "robust",
        rolling_window: int = 60,          # 滚动窗口大小（仅用于'rolling'方法）
        feature_range: tuple = (-1, 1),    # MinMaxScaler的缩放范围
        use_log_transform: bool = True,    # 是否对非负特征做对数变换
        handle_outliers: str = "clip",     # 异常值处理：'clip'（截断）/'mark'（标记）
        pca_n_components: float = 1.0,     # PCA降维（1.0表示保留全部特征）
        target_returns: np.ndarray | None = None  # 目标收益率（用于特征选择）
    ):
        """
        ## 量化交易特征处理函数，整合归一化、异常值处理、特征变换和降维
        - 存储特征处理的各项参数，后续调用get_signal_features时会使用这些参数处理特征
        Args:
            features: 原始特征数组，形状为(时间步, 特征数)
            normalize_method: 归一化方法：
                - 'standard': 均值为0、标准差为1（对极端值敏感）
                - 'robust': 中位数为0、四分位距为1（抗极端值）
                - 'minmax': 缩放到指定范围（保留相对大小）
                - 'rolling': 滚动窗口内标准化（避免未来数据泄露）
            rolling_window: 滚动窗口大小（仅当normalize_method='rolling'时有效）
            feature_range: MinMaxScaler的缩放范围（仅当normalize_method='minmax'时有效）
            use_log_transform: 是否对非负特征应用对数变换（压缩长尾分布）
            handle_outliers: 异常值处理方式：
                - 'clip': 截断到合理范围（四分位法）
                - 'mark': 新增异常值标记特征（0/1）
            pca_n_components: PCA降维参数（0~1表示保留信息量比例，>1表示保留特征数）
            target_returns: 目标收益率数组（用于过滤低相关特征，可选）

        Returns:
            处理后的特征数组，形状为(时间步, 处理后特征数)
        """
        from functools import partial
        self.__process_quant_features = partial(self._process_quant_features,
                                                normalize_method=normalize_method,
                                                rolling_window=rolling_window,
                                                feature_range=feature_range,
                                                use_log_transform=use_log_transform,
                                                handle_outliers=handle_outliers,
                                                pca_n_components=pca_n_components,
                                                target_returns=target_returns)

    def __process_quant_features(
        self,
        # 归一化方法：'standard'/'robust'/'minmax'/'rolling'
        normalize_method: Literal['standard',
                                  'robust', 'minmax', 'rolling'] = "robust",
        rolling_window: int = 60,          # 滚动窗口大小（仅用于'rolling'方法）
        feature_range: tuple = (-1, 1),    # MinMaxScaler的缩放范围
        use_log_transform: bool = True,    # 是否对非负特征做对数变换
        handle_outliers: str = "clip",     # 异常值处理：'clip'（截断）/'mark'（标记）
        pca_n_components: float = 1.0,     # PCA降维（1.0表示保留全部特征）
        target_returns: np.ndarray | None= None  # 目标收益率（用于特征选择）
    ) -> np.ndarray:
        """
        ## 量化特征处理的实际执行方法

        - 根据是否已通过partial绑定参数，决定直接调用或传入参数调用底层处理函数
        - 返回处理后的特征数组
        """
        if isinstance(self._process_quant_features, partial):
            return self._process_quant_features()
        return self._process_quant_features(normalize_method, rolling_window, feature_range, use_log_transform, handle_outliers, pca_n_components, target_returns)

    def data_enhancement(self, obs: np.ndarray, rate: float = 0.5) -> np.ndarray:
        """## 随机应用一种数据增强方法（概率由rate控制）

        - 用于强化学习中的数据增强，防止模型过拟合。首次调用时加载增强函数库，
        - 之后按概率随机选择一种增强方法对输入特征进行处理

        Args:
            obs (np.ndarray): 输入的特征数据（多维数组）
            rate (float): 应用增强的概率，默认0.5

        Returns:
            np.ndarray: 处理后的一维特征数组（增强后或原数组展平）
        """
        if not self._if_data_enhancement:
            from ..data_enhancement import data_enhancement_funcs
            self._data_enhancement_funcs = data_enhancement_funcs
            self._if_data_enhancement = True
        # 随机应用一种自毁式增强（50%概率）
        if np.random.rand() < rate:
            # 随机选择一种增强方法
            augment_func = np.random.choice(self._data_enhancement_funcs)
            return augment_func(obs)
        return obs.flatten()

    @staticmethod
    def _rl_step_decorator(step_func):
        """
        ## RL step 装饰器，自动递增 btindex

        RL 训练模式下不走 next() 循环，btindex 不会自动递增。
        此装饰器在每次 step() 调用后自动将 _btindex +1，
        确保 btindex 与实际推进的 bar 数保持同步。

        注意：step_func 是已绑定的方法（bound method），wrapper 不需要 self 参数。
        """
        from functools import wraps

        @wraps(step_func)
        def wrapper(action):
            result = step_func(action)  # bound method 调用自动传入 self
            if step_func.__self__.train:
                step_func.__self__._btindex += 1  # 通过 __self__ 访问实例
            return result
        return wrapper

    def set_model_params(
            self,
            agent=None,
            train: bool = True,
            continue_train: bool = False,
            random_policy_test: bool = False,
            window_size: int = 10,
            env_name: str = "",
            num_envs: int = 1,
            max_step: int = 0,
            state_dim: int = 0,
            action_dim: int = 0,
            if_discrete: bool = True,
            break_step: int = 1e6,
            batch_size: int = 128,
            horizon_len: int = 2048,
            buffer_size: int = None,
            repeat_times: float = 8.0,
            if_use_per: bool = False,
            gamma: float = 0.985,
            reward_scale: float = 1.,
            net_dims: tuple[int] = (64, 32),
            learning_rate: float = 6e-5,
            weight_decay: float = 1e-4,
            clip_grad_norm: float = 0.5,
            state_value_tau: float = 0.,
            soft_update_tau: float = 5e-3,
            save_gap: int = 8,
            ratio_clip: float = 0.25,
            lambda_gae_adv: float = 0.95,
            lambda_entropy: float = 0.01,
            eps: float = 1e-5,
            momentum: float = 0.9,
            lr_decay_rate: float = 0.999,
            gpu_id: int = 0,
            num_workers: int = 4,
            num_threads: int = 8,
            random_seed: int | None = 42,
            learner_gpu_ids: tuple[int] = (),
            cwd: str | None = None,
            if_remove: bool = True,
            break_score: float = np.inf,
            if_keep_save: bool = True,
            if_over_write: bool = False,
            if_save_buffer: bool = False,
            eval_times: int = 3,
            eval_per_step: int = 2e4,
            eval_env_class=None,
            eval_env_args=None,
            eval_record_step: int = 0,
            Loss=None,
            Optim=None,
            LrScheduler=None,
            SWA=None,
            Activation=None,
            swa_start_epoch_progress: float = 0.8,
            Norm=None,
            dropout_rate: float = 0.0,
            bias: bool = True,
            actor_path: str = "",
            actor_name: str = "",
            file_extension: str = ".pth",
            auto_process_features: bool = True,
            **params):
        """## 配置强化学习模型参数并初始化训练环境
        - 本方法用于设置强化学习算法的各项参数，包括环境配置、网络结构、训练超参数、
        - 优化器设置等，并初始化强化学习训练配置对象。支持多种强化学习算法和自定义组件。

        ### 📘 **文档参考**:
        - https://www.minibt.cn/minibt_reinforcement_learning/4.1reinforcement_learning_for_quantitative_trading_with_minibt/

        Args:
            agent (str, optional): 强化学习算法名称。默认为 None
            train (bool, optional): 是否进入训练模式。默认为 True
            continue_train (bool, optional): 是否继续之前的训练。默认为 False
            random_policy_test (bool, optional): 是否进行随机策略测试。默认为 False
            window_size (int, optional): 状态观察窗口大小。默认为 10
            env_name (str, optional): 环境名称。默认为空字符串
            num_envs (int, optional): 并行环境数量。默认为 1
            max_step (int, optional): 最大步数限制。默认为 0（使用数据集最大长度）
            state_dim (int, optional): 状态空间维度。默认为 0（自动计算）
            action_dim (int, optional): 动作空间维度。默认为 0（自动计算）
            if_discrete (bool, optional): 是否为离散动作空间。默认为 True
            break_step (int, optional): 训练中断步数。默认为 1e6
            batch_size (int, optional): 训练批次大小。默认为 128
            horizon_len (int, optional): 经验收集长度。默认为 2048
            buffer_size (int, optional): 经验回放缓冲区大小。默认为 None（使用数据集长度）
            repeat_times (float, optional): 策略更新重复次数。默认为 8.0
            if_use_per (bool, optional): 是否使用优先经验回放。默认为 False
            gamma (float, optional): 折扣因子。默认为 0.985
            reward_scale (float, optional): 奖励缩放因子。默认为 1.0
            net_dims (tuple[int], optional): 神经网络隐藏层维度。默认为 (64, 32)
            learning_rate (float, optional): 学习率。默认为 6e-5
            weight_decay (float, optional): 权重衰减系数。默认为 1e-4
            clip_grad_norm (float, optional): 梯度裁剪阈值。默认为 0.5
            state_value_tau (float, optional): 状态价值函数平滑参数。默认为 0.0
            soft_update_tau (float, optional): 目标网络软更新参数。默认为 5e-3
            save_gap (int, optional): 模型保存间隔（回合数）。默认为 8
            ratio_clip (float, optional): PPO算法裁剪比率。默认为 0.25
            lambda_gae_adv (float, optional): GAE优势估计参数。默认为 0.95
            lambda_entropy (float, optional): 熵奖励系数。默认为 0.01
            eps (float, optional): 数值稳定性参数。默认为 1e-5
            momentum (float, optional): 动量参数。默认为 0.9
            lr_decay_rate (float, optional): 学习率衰减率。默认为 0.999
            gpu_id (int, optional): 使用的GPU设备ID。默认为 0
            num_workers (int, optional): 数据加载工作线程数。默认为 4
            num_threads (int, optional): 计算线程数。默认为 8
            random_seed (int, optional): 随机种子。默认为 42
            learner_gpu_ids (tuple[int], optional): 学习者GPU设备ID列表。默认为空元组
            cwd (str, optional): 工作目录路径。默认为 None
            if_remove (bool, optional): 是否移除旧模型文件。默认为 True
            break_score (float, optional): 训练中断分数阈值。默认为无穷大
            if_keep_save (bool, optional): 是否保存检查点。默认为 True
            if_over_write (bool, optional): 是否覆盖已有模型。默认为 False
            if_save_buffer (bool, optional): 是否保存经验缓冲区。默认为 False
            eval_times (int, optional): 评估次数。默认为 3
            eval_per_step (int, optional): 评估间隔步数。默认为 2e4
            eval_env_class (type, optional): 评估环境类。默认为 None
            eval_env_args (dict, optional): 评估环境参数。默认为 None
            eval_record_step (int, optional): 评估记录步数。默认为 0
            Loss (class, optional): 自定义损失函数类。默认为 None
            Optim (class, optional): 自定义优化器类。默认为 None
            LrScheduler (class, optional): 自定义学习率调度器类。默认为 None
            SWA (class, optional): 随机权重平均类。默认为 None
            Activation (class, optional): 自定义激活函数类。默认为 None
            swa_start_epoch_progress (float, optional): SWA开始训练进度。默认为 0.8
            Norm (class, optional): 归一化层类。默认为 None
            dropout_rate (float, optional): Dropout比率。默认为 0.0
            bias (bool, optional): 是否使用偏置项。默认为 True
            actor_path (str, optional): 预训练Actor模型路径。默认为空字符串
            actor_name (str, optional): Actor模型名称。默认为空字符串
            file_extension (str, optional): 模型文件扩展名。默认为 ".pth"
            **params: 其他关键字参数

        Returns:
            Config: 强化学习训练配置对象，包含所有设置的参数

        Raises:
            AssertionError: 当指定的agent不在支持的算法列表中时抛出

        ### Note:
            - 本方法会自动计算状态和动作维度，无需手动指定
            - 支持多种强化学习算法，包括PPO、DDPG、SAC等
            - 提供丰富的自定义选项，包括网络结构、优化器、损失函数等
            - 设置完成后，返回的Config对象可直接用于训练过程
        """
        if self._first_start:
            return None
        kwargs = locals()
        params = kwargs.pop("params", {})
        agent = kwargs.pop('agent', None)
        from minibt.elegantrl.agents import Agents
        assert agent in Agents, f"强化学习算法必须在以下算法中：{Agents}"
        kwargs.pop("self")
        self.train = train
        self.window_size = kwargs.pop("window_size", 10)
        kwargs = {**kwargs, **params}
        self.__auto_process_features = kwargs.pop("auto_process_features", True)
        if self.__auto_process_features:
            self.get_signal_features()  # 自动处理特征（归一化、异常值、PCA等）
        else:
            self.get_raw_indicator_features()  # 仅做基础缺失值处理
        state, _ = self.reset()
        env_name = kwargs.pop('env_name', "")
        num_envs = kwargs.pop('num_envs', 1)
        max_step = kwargs.pop('max_step', 0)
        state_dim = kwargs.pop('state_dim', 0)
        action_dim = kwargs.pop('action_dim', 0)
        if_discrete = kwargs.pop('if_discrete', True)
        # 环境名称默认使用策略类名
        env_name = env_name if isinstance(
            env_name, str) and env_name else f"{self.__class__.__name__}Env"
        num_envs = int(num_envs) if isinstance(
            num_envs, (int, float)) and num_envs >= 1 else 1
        max_step = int(max_step) if isinstance(
            max_step, (int, float)) and max_step >= 1 else self._btklinedataset.max_length
        max_step -= 1
        action_dim = int(action_dim) if isinstance(
            action_dim, (float, int)) and action_dim >= 1 else 1
        state_dim = int(state_dim) if isinstance(
            state_dim, (float, int)) and state_dim >= 1 else state.shape[0]
        if_discrete = bool(if_discrete)
        self._env_args = {
            'env_name': env_name,
            'num_envs': num_envs,
            'max_step': max_step,
            'state_dim': state_dim,
            'action_dim': action_dim,
            'if_discrete': if_discrete,
        }
        self.step = self._rl_step_decorator(self.step)
        try:
            import torch as th
        except ImportError:
            raise ImportError(
                "torch 未安装，请运行: pip install torch\n"
                "或参考: https://pytorch.org/get-started/locally/"
            )
        self.th = th
        from minibt.elegantrl.train.config import Config
        self._rl_config = Config(agent, self, self._env_args, cwd)
        kwargs.pop("cwd")
        # 将参数设置到配置对象
        for k, v in kwargs.items():
            setattr(self._rl_config, k, v)
        # 缓冲区大小默认使用数据集长度
        if self._rl_config.buffer_size is None:
            self._rl_config.buffer_size = self._btklinedataset.max_length
        from ..rl_utils import Optim, Loss, Activation
        # 设置默认损失函数、优化器和激活函数
        if self._rl_config.Loss is None:
            self._rl_config.Loss = Loss.MSELoss(reduction="none")
        if self._rl_config.Optim is None:
            self._rl_config.Optim = Optim.AdamW(
                eps=eps, weight_decay=weight_decay,)
        if self._rl_config.Activation is None:
            self._rl_config.Activation = Activation.Tanh10()
        return self._rl_config
    
    def step_result(self, reward: float,terminated: bool=False)-> tuple[np.ndarray, float, bool, bool, dict[str, Any]]:
        """构造 gymnasium 风格的 step 返回值
        Returns:
            observation: 下一个状态
            reward: 当前步的奖励
            truncated: 是否达到截断条件（超出 MDP 范围，如步数上限）
            terminated: 是否达到终止条件（MDP 自然结束）
            info: 辅助诊断信息
        """
        return self.next_state, reward, self.done, terminated, {}

    def random_policy_test(self):
        """
        ## 随机策略测试，用于验证环境和动作空间的有效性

        - 该方法通过随机采样动作来与环境交互，输出总奖励、步数和动作分布统计，
        - 可用于判断环境是否正常工作以及动作空间设置是否合理
        """
        import warnings
        warnings.filterwarnings("ignore", category=UserWarning, module="gym")
        if_discrete = self._env_args.get('if_discrete', True)
        from gym import spaces
        # 根据动作类型（离散/连续）定义动作空间
        if if_discrete:
            action_space = spaces.Discrete(self.action_dim)
        else:
            action_space = spaces.Box(
                low=-1., high=1., shape=(self.action_dim,), dtype=np.float32)

        self.reset()
        rewards = 0.
        actions = []
        # 与环境交互，执行随机动作
        for i in range(self.min_start_length, self.max_step):
            action = action_space.sample()
            if if_discrete:
                actions.append(action)
            else:
                action = np.clip(action, action_space.low, action_space.high)
                actions.append(action.tolist())
            state, reward, terminal, truncated, info_dict = self.step(action)
            done = terminal or truncated
            rewards += reward
            if done:
                print(
                    f"Random Policy Test: Final Reward {rewards:.2f} ,Total Steps {i} ,max_steps {self.max_step}")
                break
        # 输出动作分布统计（连续动作输出均值、标准差等；离散动作输出计数）
        if not if_discrete:
            actions = np.array(actions)
            actions = actions.reshape(-1, self._env_args['action_dim'])
            actions = np.clip(actions, action_space.low, action_space.high)
            actions = pd.DataFrame(
                actions, columns=[f'action_{i}' for i in range(self._env_args['action_dim'])])
            actions.describe().loc[['mean', 'std', 'min', 'max']]
            print(
                f"Random Policy Test: Action Distribution:\n{actions.describe().loc[['mean', 'std', 'min', 'max']]}")
            return
        print(
            f"Random Policy Test: Action Distribution :{pd.Series(actions).value_counts()}")

    def train_agent(self,*args, **kwargs):
        """## 启动强化学习智能体训练，调用底层训练函数"""
        from minibt.elegantrl.train.run import train_agent
        train_agent(self._rl_config, True)

    def _get_actor(self, map_location=None, weights_only=None):
        """
        ## 加载训练好的智能体模型（actor）

        - 从指定路径或默认模型目录加载模型文件，初始化actor网络并加载权重，
        - 用于后续的推理或继续训练

        Args:
            map_location: 模型加载的设备（如cpu、cuda:0）
            weights_only: 是否只加载权重（忽略其他状态）

        Returns:
            加载好权重的actor模型
        """
        import os
        SEED = self._rl_config.random_seed
        self.th.manual_seed(SEED)       # PyTorch随机种子
        np.random.seed(SEED)          # Numpy随机种子
        self.th.backends.cudnn.deterministic = True  # CuDNN确定性模式
        self.th.backends.cudnn.benchmark = False      # 关闭Benchmark加速（避免随机性）
        # 确定模型路径：优先使用指定路径，否则从模型目录查找
        if os.path.exists(self._rl_config.actor_path):
            actor_path = self._rl_config.actor_path
        else:
            cwd = self._rl_config.model_cwd
            assert cwd and isinstance(
                cwd, str), "cwd 必须是有效的路径字符串"
            assert os.path.exists(cwd), f"路径不存在: {cwd}"
            file_extension: str = self._rl_config.file_extension
            if not file_extension.startswith('.'):
                file_extension = '.' + file_extension
            if file_extension not in ['.pth', '.pt']:
                raise ValueError(
                    f"文件扩展名必须是 '.pth' 或 '.pt'，得到的是 {file_extension}")
            if file_extension == '.pt':
                from minibt.other import get_sorted_pth_files
                actor_path = get_sorted_pth_files(
                    cwd, file_extension)[0]
            else:
                from minibt.other import find_pth_files
                try:
                    actor_path = find_pth_files(cwd)[0]
                except Exception as e:
                    print(e)
                    if not self.train:
                        raise IOError("非训练模式，找不到文件，请先训练模型，然后加载！")
        assert os.path.exists(actor_path), f"路径错误：{actor_path}"
        print(f"| actor路径: {actor_path}")
        args = self._rl_config
        # 初始化actor网络
        actor = args.agent_class(
            args.net_dims, args.state_dim, args.action_dim, gpu_id=args.gpu_id, args=args).act
        actor.to(map_location)  # 移动到目标设备（如CPU/GPU）
        try:
            # 尝试加载状态字典
            state_dict = self.th.load(
                actor_path, map_location=map_location, weights_only=weights_only)

            # 检查加载的是否是完整模型而不是状态字典
            if hasattr(state_dict, 'state_dict'):
                # print("检测到完整模型对象，提取其状态字典")
                state_dict = state_dict.state_dict()

            actor.load_state_dict(state_dict)
        except TypeError:
            # 如果直接加载失败，尝试先实例化模型再提取状态字典
            # print("尝试直接加载模型对象并提取状态字典")
            loaded_model = self.th.load(actor_path, map_location=map_location)
            if hasattr(loaded_model, 'state_dict'):
                actor.load_state_dict(loaded_model.state_dict())
            else:
                raise ValueError("无法从加载的文件中获取有效的状态字典")

        self.actor = actor
        return self.actor

    def load_agent(self):
        """## 加载智能体模型用于推理"""
        self._rl_config.train = False
        self._rl_config.if_remove = False
        self._rl_config.init_before_training()
        self._get_actor(map_location="cpu", weights_only=False)
        self.actor.eval()  # 设置为评估模式
        self.actor = self.actor.float()  # 确保模型参数为float32
        self.device = next(self.actor.parameters()).device
        # 若子类重写了next方法，则将step指向next
        if self._is_method_overridden("next"):
            self.step = self.next
        self._state, _ = self.reset()  # 重置环境状态

    @classmethod
    def _is_method_overridden(cls, method_name):
        """## 检查当前类是否重写了指定的方法

        Args:
            method_name (str): 方法名称

        Returns:
            bool: 若重写则返回True，否则返回False"""
        import types
        return method_name in cls.__dict__ and isinstance(cls.__dict__[method_name], types.FunctionType)

    def _start_end_datetime(self) -> tuple[str]:
        """获取所有交易数据的开始和结束时间"""
        starts, ends = [], []
        for _, kline in self._btklinedataset.items():
            _datetime = kline.datetime.values
            start, end = _datetime[0], _datetime[-1]
            starts.append(start)
            ends.append(end)

        # 修正：计算正确的开始和结束时间
        start_time = min(starts) if len(starts) > 1 else starts[0]
        end_time = max(ends) if len(ends) > 1 else ends[0]

        # 使用pd.Timestamp转换并格式化
        start_time = pd.Timestamp(start_time).strftime('%Y-%m-%d %H:%M:%S')
        end_time = pd.Timestamp(end_time).strftime('%Y-%m-%d %H:%M:%S')

        return start_time, end_time


class default_strategy(Strategy):
    """## 默认策略类，继承自Strategy"""
    config = btconfig(islog=False, profit_plot=False)

    def __init__(self) -> None:
        self.kline = self.get_kline(LocalDatas.test,height=400)

