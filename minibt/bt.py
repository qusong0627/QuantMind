from __future__ import annotations
from .strategy.strategy import Strategy
from .utils import (
    MAX_WORKERS, Literal, Base, pd,
    Iterable, flatten, FILED,
    _time, Addict, tq_account,
    tq_auth, TYPE_CHECKING, os,
    FilteredOutputRedirector)

if TYPE_CHECKING:
    from .typing_ import *
    from .utils import GAOpConfig, OptunaConfig
    from .strategy.stats import Stats
    from .strategy.qs_plots import QSPlots
    from bokeh.models import Tabs


def _run_single_strategy_process(strategy_class, strategy_id: int, period_ms: int,
                                  auth_values: dict, account_values: dict | None,
                                  base_datetime: float | None = None, datakey: str | None = None):
    """子进程入口：独立运行单个策略（每个进程独占一个 TqApi 连接）

    Args:
        strategy_class: 策略类（非实例）
        strategy_id: 策略编号
        period_ms: 数据更新间隔（毫秒）
        auth_values: 天勤认证参数 {"user_name": ..., "password": ...}
        account_values: 天勤实盘账户参数（None=模拟盘）
        base_datetime: 基准 datetime 值（可选，对齐多策略起始时间）
        datakey: 单合约缓存文件标识（可选，避免多进程文件冲突）
    """
    from .utils import tq_auth, tq_account

    bt = Bt(live=True)
    bt.addstrategy(strategy_class)

    if account_values:
        bt.addTqapi(
            tq_auth=tq_auth(**auth_values),
            tq_account=tq_account(**account_values),
        )
    else:
        bt.addTqapi(tq_auth=tq_auth(**auth_values))

    bt.run(isplot=False, period_milliseconds=period_ms)


class Bt:
    """
    ## 轻量级量化回测与实盘框架（minibt）核心类

    ### 📘 **文档参考**:
    - 类简介：https://www.minibt.cn/minibt_basic/1.7minibt_bt_class_intro/
    - K线回放：https://www.minibt.cn/minibt_basic/1.15minibt_kline_replay/
    - 实时图表：https://www.minibt.cn/minibt_basic/1.20minibt_lightweight_charts/

    ### 核心功能：
    - 1. 数据管理：手动/自动加载回测数据
    - 2. 策略管理：加载自定义策略、默认策略
    - 3. 实盘对接：天勤TqApi实盘/模拟盘连接
    - 4. 参数优化：支持遗传算法（GA）和贝叶斯优化（Optuna）
    - 5. 回测执行：单策略/多策略并行回测
    - 6. 结果分析：Bokeh可视化、QuantStats性能报告

    ### 标准使用流程：
    - → 1. 初始化Bt实例 
    - → 2. 添加数据（adddata）/策略（addstrategy）/实盘API（addTqapi）
    - → 3. （可选）参数优化（optstrategy） 
    - → 4. 执行回测/实盘（run） 
    - → 5. 结果分析（画图/报告）

    Args：
    >>> auto (bool): 是否自动加载全局资源（推荐生产环境手动添加，调试时可开启）
            - True：自动扫描全局变量中的有效回测数据（DataFrame）和天勤TqApi实例
            - False：仅使用手动添加的资源（addata/addTqapi等方法）
        live (bool): 是否启用实盘模式（默认False，即回测模式）
            - True：连接实盘/模拟盘，执行实时交易逻辑
            - False：基于历史数据执行回测
        replay (bool): 是否启用回放模式（用于策略回放测试，默认False）
        kwargs: 额外配置参数
            - quick_live (dict): 快速实盘配置字典，含'live'键（控制是否进入实盘模式）

    Examples:
    >>> if __name__ == "__main__":
            Bt().run()

    """
    DATAS_ERROR = "传入数据必须为 pandas.DataFrame 类型"  # 数据类型错误提示
    STRATEGY_ERROR = "传入策略必须为 Strategy 基类的子类"  # 策略类型错误提示
    instances: list = []  # 类属性：存储所有Bt实例（用于全局策略自动查找）

    def __init__(self, auto=True, live=False, replay=False, **kwargs) -> None:
        """
        初始化minibt回测/实盘框架实例，完成核心资源初始化与环境配置

        参数说明：
            auto (bool): 是否自动加载全局资源（推荐生产环境手动添加，调试时可开启）
                - True：自动扫描全局变量中的有效回测数据（DataFrame）和天勤TqApi实例
                - False：仅使用手动添加的资源（addata/addTqapi等方法）
            live (bool): 是否启用实盘模式（默认False，即回测模式）
                - True：连接实盘/模拟盘，执行实时交易逻辑
                - False：基于历史数据执行回测
            replay (bool): 是否启用回放模式（用于实策略回放测试，默认False）
        ** kwargs: 额外配置参数
                - quick_live (dict): 快速实盘配置字典，含'live'键（控制是否进入实盘模式）

        核心属性初始化：
            - 运行状态：记录框架启动时间（__start_time，用于统计总耗时）、回测/优化完成状态（__is_finish）、
            参数优化开关（__isoptimize）
            - 数据管理：存储回测数据列表（__datas，元素为pandas.DataFrame）
            - 策略管理：存储未实例化的策略类列表（strategy）、策略参数列表（__params）、多策略数量标记（__multi_num）
            - 实盘对接：天勤TqApi实例（_api，初始未连接）、实盘模式开关（__live）
            key为合约简称，value为全称，实盘时用于合约映射）

        自动加载资源逻辑（仅当auto=True时触发）：
            1. 回测数据加载（非实盘模式）：
                - 扫描全局变量中的DataFrame对象，筛选包含所有必要字段（FILED.ALL）的数据
                - 为符合条件的数据自动绑定全局变量名（便于识别），并存入__datas列表
            2. TqApi实例加载（实盘模式）：
                - 屏蔽天勤TqApi初始化时的冗余日志（通过重定向stdout实现）
                - 从全局变量中查找已初始化的TqApi实例，若存在则赋值给_api属性
        """
        self.__start_time = _time.time()  # 框架启动时间（用于统计耗时）
        self.strategies: list[Strategy] = []  # 存储策略类（未实例化）
        self.__multi_num: int = 1  # 多策略数量标记（默认单策略）
        self.__is_finish: bool = False  # 回测/优化是否完成（初始未完成）
        self.__isoptimize: bool = False  # 是否开启参数优化（初始关闭）
        self._api = None  # 天勤TqApi实例（实盘/模拟盘连接，初始未初始化）
        self.__live: bool = live  # 是否进入实盘模式（初始关闭）
        self.__params: list = []  # 策略参数列表（与策略类一一对应）
        self.__replay: bool = replay

        self.__auto = bool(auto)  # 自动加载资源开关
        self.__quick_live = kwargs.pop('quick_live', {})  # 快速实盘配置
        self.__datas: list[pd.DataFrame] = []  # 存储回测数据（DataFrame列表）

        # 自动加载资源（仅当auto=True时执行）
        if self.__auto:
            import gc
            import sys
            glos = sys._getframe(1).f_globals

            # 1. 自动加载回测数据（非实盘模式）
            if (not self.__quick_live) or (self.__quick_live and not self.__quick_live.get('live')):
                # 从全局对象中筛选符合要求的DataFrame（必须包含所有必要字段FILED.ALL）
                data_list = [obj for obj in gc.get_objects()
                             if isinstance(obj, pd.DataFrame) and set(obj.columns).issuperset(FILED.ALL)]
                if data_list:
                    # 给每个DataFrame设置名称（对应全局变量名，便于识别）
                    data_id = [id(data) for data in data_list]
                    for name, obj in glos.items():
                        if id(obj) in data_id:
                            obj.name = name
                    self.__datas = data_list  # 保存自动加载的数据

            # 2. 自动加载TqApi实例（实盘模式）
            if (not self.__quick_live) or (self.__quick_live and self.__quick_live.get('live')):
                with FilteredOutputRedirector():
                    from tqsdk import TqApi  # 导入天勤API
                # 从全局对象中查找已初始化的TqApi实例
                api = [k for k in gc.get_referrers(
                    TqApi) if k.__class__ == TqApi]
                if api:
                    self._api = api[0]  # 保存TqApi实例

    def addTqapi(self, tq_auth: tq_auth = None, tq_account: tq_account | None = None, api=None) -> Bt:
        """## 添加天勤实盘/模拟盘API连接（手动初始化TqApi）
        ---

        Args:
        ----
            tq_auth (_tq): 天勤用户认证信息（含username、password）
            tq_account (_tq, optional): 天勤实盘账户信息（含broker_id、account_id、password）
                                        - 非None：连接实盘账户
                                        - None：连接模拟盘（默认）
            api (TqApi, optional): 当无账户信息时可传入天勤API
        """
        with FilteredOutputRedirector():
            # 导入天勤API相关类（实盘账户、模拟盘、认证）
            from tqsdk import TqApi, TqAuth, TqKq, TqAccount
            if isinstance(api, TqApi):
                self._api = api
                return self
            # 保存认证信息供并行模式子进程创建独立 TqApi
            if tq_auth is not None:
                self._tq_auth_values = tq_auth.values
            if tq_account is not None:
                self._tq_account_values = tq_account.values
            if tq_account is not None:
                # 实盘模式：使用TqAccount初始化（需实盘账户信息）
                self._api = TqApi(
                    TqAccount(**tq_account.values),  # 解包实盘账户参数
                    auth=TqAuth(**tq_auth.values)     # 解包认证参数
                )
            else:
                # 模拟盘模式：使用TqKq初始化（无需实盘账户）
                self._api = TqApi(
                    TqKq(),  # 模拟盘标识
                    auth=TqAuth(**tq_auth.values)  # 解包认证参数
                )
        return self
    
    def download(self, symbol: str | list[str],duration_seconds: int, data_length: int = 10000, adj_type: str | None = None,path:str="./datas", **kwargs: Any) -> list[pd.DataFrame]:
        """
        ## 从天勤API下载合约数据
        ---

        Args:
            symbol (str): 合约代码
            duration_seconds (int): 数据时间范围（秒）
            data_length (int, optional): 数据长度（默认1000）
            adj_type (str, optional): 数据调整类型（默认None）
            path (str, optional): 数据保存路径（默认"./datas"）
            **kwargs: 其他天勤API参数
        Returns:
            list[pd.DataFrame]: DataFrame数据列表
        """
        if self._api is None:
            print("请先添加天勤API连接")
            return []
        if isinstance(symbol, str):
            symbol = [symbol,]
        assert all(isinstance(s,str) for s in symbol), "symbol 必须是字符串或字符串列表"
        assert isinstance(duration_seconds,int), "duration_seconds 必须是整数"
        # 如果path路径不存在，则创建文件夹
        os.makedirs(path, exist_ok=True)
        dfs:list[pd.DataFrame]=[]
        try:
            for s in symbol:
                dfs.append(self._api.get_kline_serial(s, duration_seconds, data_length, adj_type, **kwargs))
            for i,df in enumerate(dfs):
                name = symbol[i].replace(".","_")
                df.to_csv(os.path.join(path,f"{name}_{duration_seconds}.csv"),index=False)
        except Exception as e:
            print(f"下载数据失败：{e}")
            import traceback
            traceback.print_exc()
        self._api.close()
        return dfs



    def adddata(self, *args, **kwargs: dict[str, pd.DataFrame]) -> Bt:
        """
        ## 向回测系统添加数据，支持多种数据传入方式
        - 添加的据将存储在内部数据列表中，可用于后续回测分析。

        ### 该方法支持两种数据传入格式：
        - 1. 位置参数 (*args): 一个或多个未命名的DataFrame数据
        - 2. 关键字参数 (**kwargs): 多个已命名的DataFrame数据，格式为 name=DataFrame

        Args:
            *args: 可变位置参数，可接收一个或多个DataFrame对象。
                每个DataFrame必须包含回测所需的完整字段（由FILED.ALL定义）。
                这些数据会按顺序自动命名为"data0"、"data1"等。

            **kwargs: 可变关键字参数，接收以名称作为键、DataFrame作为值的字典。
                    键将作为数据的名称存储在DataFrame.name属性中。
                    例如：adddata(us_stock=df1, hk_stock=df2)

        Returns:
            Bt: 返回Bt实例自身，支持链式调用。

        Raises:
            ValueError: 当未传入任何数据时抛出
            TypeError: 当传入的数据不是DataFrame类型时抛出

        Examples:
            >>> bt = Bt()
            >>> df1 = pd.DataFrame(...)
            >>> df2 = pd.DataFrame(...)

            # 方式1: 使用位置参数
            >>> bt.adddata(df1, df2)

            # 方式2: 使用关键字参数命名数据
            >>> bt.adddata(main_data=df1, aux_data=df2)

            # 方式3: 混合使用
            >>> bt.adddata(df1, benchmark=df2)

            # 链式调用
            >>> bt.adddata(df1).adddata(df2).run()
        """
        # 校验：至少传入一组数据
        if not kwargs and not args:
            raise ValueError("添加数据失败：未传入任何数据")

        if args:
            kwargs.update({f"data{i}": v for i, v in enumerate(args)})

        # 校验：每组数据必须是DataFrame类型，符合要求则添加到数据列表
        for name, value in kwargs.items():
            if not isinstance(value, pd.DataFrame):
                raise TypeError(f"{self.DATAS_ERROR}，当前传入类型：{type(value)}")
            value.name = name
            self.__datas.append(value)

        return self

    def _load_strategies(self, path):
        """自动从 .py 文件或文件夹中发现并加载所有 Strategy 子类。

        支持四种路径格式：
        - 目录路径（扫描 *.py）
        - .py 文件路径
        - 纯模块名（自动补 .py，在当前目录查找）
        - Python 包导入路径（点号分隔，如 'tutorials.strategy.cci'）
        """
        import importlib
        import importlib.util
        from pathlib import Path
        import sys

        path_obj = Path(path)
        strategy_classes = []
        py_files = []

        # ---- 判断路径类型 ----
        is_dot_path = ('.' in path and not path_obj.exists()
                       and path_obj.suffix and path_obj.suffix != '.py')

        if is_dot_path:
            # ---- 方式四：Python 包导入路径（如 'tutorials.strategy.cci'） ----
            # 还原为文件系统路径：'tutorials.strategy.cci' → 'tutorials/strategy/cci.py'
            parts = path.split('.')
            file_path = Path(*parts[:-1]) / f"{parts[-1]}.py"
            if file_path.exists():
                py_files = [file_path]
            else:
                # 尝试作为模块直接导入（需在 sys.path 中）
                try:
                    module = importlib.import_module(path)
                    self._collect_strategy_classes(module, strategy_classes)
                except ModuleNotFoundError:
                    print(f"[警告] 无法导入模块: {path}")
                except Exception as e:
                    print(f"[警告] 导入模块 {path} 失败: {e}")
                return strategy_classes

        elif path_obj.is_dir():
            py_files = sorted(
                p for p in path_obj.glob("*.py")
                if p.stem != "__init__"
            )
        elif path_obj.suffix == ".py":
            py_files = [path_obj]
        else:
            candidate = path_obj.with_suffix(".py")
            py_files = [candidate if candidate.exists() else path_obj]

        # ---- 逐个文件导入并查找 Strategy 子类 ----
        for py_file in py_files:
            module_name = py_file.stem
            try:
                file_dir = str(py_file.parent.resolve())
                if file_dir not in sys.path:
                    sys.path.insert(0, file_dir)
                module = importlib.import_module(module_name)
            except ModuleNotFoundError:
                spec = importlib.util.spec_from_file_location(
                    module_name, str(py_file.resolve()))
                if spec is None or spec.loader is None:
                    print(f"[警告] 无法导入文件: {py_file}")
                    continue
                module = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(module)
                except Exception as e:
                    print(f"[警告] 执行模块 {module_name} 失败: {e}")
                    continue

            self._collect_strategy_classes(module, strategy_classes)

        return strategy_classes

    def _collect_strategy_classes(self, module, strategy_classes: list):
        """从已导入的模块中收集所有 Strategy 子类（不去重）。"""
        for attr_name in dir(module):
            obj = getattr(module, attr_name, None)
            if (
                isinstance(obj, type)
                and issubclass(obj, Strategy)
                and obj is not Strategy
            ):
                strategy_classes.append(obj)

    def addstrategy(self, *args, **kwargs) -> Bt:
        """## 添加策略类（手动加载自定义策略，支持多策略）

        Args:
            arg: 策略类（Strategy子类）或字符串路径（.py文件 / 文件夹）
                 字符串时自动调用 _load_strategies 发现 Strategy 子类
            kwargs: 策略参数（dict，用于批量传递参数）

        Returns:
            Bt: 返回Bt实例，支持链式调用
        """
        assert args, '策略不能为空'
        for arg in args:
            if isinstance(arg, str):
                # 字符串路径：自动发现并加载 Strategy 子类
                found = self._load_strategies(arg)
                for s in found:
                    if s not in self.strategies:
                        [setattr(s, k, v) for k, v in kwargs.items()]
                        self.strategies.append(s)
            elif issubclass(arg, Strategy) and arg not in self.strategies:
                [setattr(arg, k, v) for k, v in kwargs.items()]
                self.strategies.append(arg)
        return self

    def __run_cmd(self, cmd_str: str) -> None:
        """
        私有方法：执行系统命令（用于启动实盘画图子进程）
        特性：不显示命令执行时弹出的黑框，命令输出会打印到Python控制台

        param cmd_str: 待执行的系统命令字符串（如"python live_plot.py"）
        """
        from subprocess import Popen  # 用于执行外部命令
        # # shell=False：禁用shell解析，避免安全风险（如命令注入）
        Popen(cmd_str, shell=False)

    def __tq_real(self, isplot: bool, **kwargs) -> None:
        """实时交易入口：图表模式 / 后台串行 / 多进程并行"""
        assert self._api, '请先添加天勤API'
        run_parallel = kwargs.pop('run_parallel', False)
        period_milliseconds = kwargs.pop(
            'period_milliseconds', 1000)  # 数据更新频率（默认1000ms）
        if not (isinstance(period_milliseconds, int) and period_milliseconds >= 0):
            period_milliseconds = 1000

        # 若策略包含强化学习（RL），禁用梯度计算（避免显存占用）
        if any([s.rl for s in self.strategies]):
            from torch import no_grad
            no_grad()  # 上下文管理器：禁用PyTorch梯度计算
        
        if isplot:
            # 图表模式：实例化策略并启动 Qt GUI
            for i, s in enumerate(self.strategies):
                self.strategies[i] = s(_sid=i, _isplot=isplot)
                self.strategies[i]._prepare_before_strategy_start()
                self.strategies[i]._get_plot_datas()
                self.strategies[i]._first_start = True
                if s.rl:
                    self.strategies[i].load_agent()
            from .strategy.light_chart import main
            main(self.strategies, period_milliseconds)

        elif run_parallel and len(self.strategies) > 1:
            # 并行模式：每个策略一个独立进程（不需要父进程实例化策略）
            self.__tq_real_parallel(period_milliseconds)

        else:
            # 后台串行模式：实例化所有策略，单进程循环执行
            for i, s in enumerate(self.strategies):
                self.strategies[i] = s(_sid=i, _isplot=isplot)
                self.strategies[i]._prepare_before_strategy_start()
                self.strategies[i]._get_plot_datas()
                self.strategies[i]._first_start = True
                if s.rl:
                    self.strategies[i].load_agent()
            self.__tq_real_headless(period_milliseconds)

    def __tq_real_headless(self, period_milliseconds: int = 1000) -> None:
        """纯后台实盘交易循环（无图表、无 GUI）"""
        import time
        import traceback

        print("=" * 50)
        print("[后台实盘模式] 已启动，不显示图表")
        print(f"策略数量: {len(self.strategies)}")
        print("=" * 50)
        first_start = True
        while True:
            try:
                # 等待行情更新（所有策略共用同一 TqApi）
                is_open = self.strategies[0].wait_update(1)

                if first_start and is_open:
                    first_start = False

                if not is_open:
                    # 收盘/无新数据，继续等待
                    time.sleep(period_milliseconds / 1000.0)
                    continue

                # 执行所有策略的交易逻辑
                for strategy in self.strategies:
                    try:
                        strategy._execute_live_trading()
                    except Exception as e:
                        print(f"[策略 {strategy.__class__.__name__}] 执行异常: {e}")
                        traceback.print_exc()

                # 可选：打印账户信息（每 10 秒一次，避免刷屏）
                # 由策略内部或外部监控控制，此处保持静默以减少日志

            except KeyboardInterrupt:
                print("\n[后台实盘模式] 收到退出信号，正在停止...")
                break
            except Exception as e:
                print(f"[后台实盘模式] 交易循环异常: {e}")
                traceback.print_exc()
                time.sleep(period_milliseconds / 1000.0)

    def __tq_real_parallel(self, period_milliseconds: int = 1000) -> None:
        """并行模式：每个策略一个独立进程，各自独占 TqApi 连接
        - 进程数 = 策略数，互不干扰
        - 任一策略崩溃不影响其他进程
        - Ctrl+C 统一终止所有子进程
        """
        import multiprocessing

        # 认证信息必须已由 addTqapi 保存（无认证无法创建子进程 TqApi）
        auth_values = getattr(self, '_tq_auth_values', None)
        if not auth_values:
            print("[并行模式] 错误：缺少天勤认证信息，请先调用 addTqapi()")
            return

        account_values = getattr(self, '_tq_account_values', None)
        strategy_names = [s.__name__ for s in self.strategies]
        num = len(self.strategies)

        print("=" * 50)
        print(f"[并行模式] 启动 {num} 个独立进程")
        for i, name in enumerate(strategy_names):
            print(f"  [{i}] {name}")
        print("=" * 50)

        # Windows 必须用 spawn（默认），避免 TqApi fork 导致的资源冲突
        processes: list[multiprocessing.Process] = []

        for i, strategy_class in enumerate(self.strategies):
            p = multiprocessing.Process(
                target=_run_single_strategy_process,
                args=(strategy_class, i, period_milliseconds,
                      auth_values, account_values),
                name=strategy_class.__name__,
            )
            p.daemon = False
            p.start()
            processes.append(p)
            print(f"[并行模式] {strategy_class.__name__} 启动成功 (PID={p.pid})")

        # 父进程等待所有子进程
        try:
            for p in processes:
                p.join()
        except KeyboardInterrupt:
            print("\n[并行模式] Ctrl+C，终止所有子进程...")
            for p in processes:
                if p.is_alive():
                    p.terminate()
            # 等待子进程释放资源（最多 5 秒）
            for p in processes:
                p.join(timeout=5)
                if p.is_alive():
                    print(f"[并行模式] {p.name} 未响应，强制 kill (PID={p.pid})")
                    p.kill()
        finally:
            # 确保没有僵尸进程
            for p in processes:
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=2)

        print("[并行模式] 所有子进程已退出")

    def _strategy_replay(self, **kwargs):
        # with open(r'd:\newminibt\debug_init_datas.txt', 'a', encoding='utf-8') as _f:
        #     _f.write("\n=== _strategy_replay 被调用 ===\n")
        from .utils import storeData, BASE_DIR
        from queue import Queue, LifoQueue
        import asyncio
        import traceback
        import time
        import subprocess
        import sys
        # 确保目录存在
        os.makedirs(f"{BASE_DIR}/liveplot", exist_ok=True)
        replay_base_dir = f"{BASE_DIR}/liveplot/replay"
        os.makedirs(replay_base_dir, exist_ok=True)
        init_datas_dir = f"{replay_base_dir}/init_datas"
        update_datas_dir = f"{replay_base_dir}/update_datas"
        update_trade_dir = f"{replay_base_dir}/trade_datas"
        account_info_dir = f"{replay_base_dir}/account_info"
        pause_status_dir = f"{replay_base_dir}/pause_status"
        id_dir = f"{BASE_DIR}/id"  # 实时账户信息路径

        snum = len(self.strategies)
        black_style = kwargs.pop('black_style', False)
        plot_width = kwargs.pop('plot_width', None)
        # 多于1秒间隔有BUG，固定为1秒更新
        # period_milliseconds = kwargs.pop("period_milliseconds", 1000)
        period_milliseconds = 1000
        cycle_interval = kwargs.pop('cycle_interval', 1)
        # ========== 多策略队列（暂存原始格式数据） ==========
        # 存储source: [sid, source_list]
        datas_queues = [LifoQueue(maxsize=1) for _ in range(snum)]
        # 存储trade: [sid, trade_list]
        trade_queues = [LifoQueue(maxsize=1) for _ in range(snum)]
        account_queues = [Queue(maxsize=1)
                          for _ in range(snum)]    # 存储info: 账户字符串
        init_datas = []
        strategy_names = []
        strategy_sids = []  # 存储每个策略的sid

        # 画图参数解析
        click_policy = kwargs.pop(
            'click_policy', self.strategies[0].config.click_policy) if self.strategies else 'hide'

        # 实例化策略并记录sid
        for i, s in enumerate(self.strategies):
            s.config.isplot = True
            s.min_start_length = max(s.min_start_length, 300)
            # _btindex 已由 min_start_length setter 正确设置为 min_start_length - 1
            # 先设置 replay 标记，再调用 _get_plot_datas，确保 K线数据和指标数据都被
            # 截断为 min_start_length，保持长度一致（指标通过 strategy_instance._strategy_replay 同步）
            s._strategy_replay = True
            s._get_plot_datas()
            s._first_start = True
            init_datas.append(s._plot_datas)
            strategy_names.append(s.__class__.__name__)
            strategy_sids.append(s.sid)  # 记录策略sid
            if s.rl:
                ...

        # 初始化数据（保持原有格式）
        storeData(init_datas, init_datas_dir)
        storeData([], update_datas_dir)  # 初始化为空列表
        storeData([], update_trade_dir)  # 初始化为空列表
        storeData([s._get_account_info()
                  for s in self.strategies], account_info_dir)
        storeData((0, 0), id_dir)  # 初始化为空列表

        # 初始化暂停状态
        try:
            with open(pause_status_dir, 'w') as f:
                f.write('0')
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            print(f"初始化暂停状态失败: {e}")

        plot_width = plot_width if isinstance(
            plot_width, int) and 800 <= plot_width <= 2400 else 1600

        # ---------------------- 暂停控制器 ----------------------
        class FilePauseController:
            def __init__(self, status_path: str, check_interval=0.5):
                self.status_path = status_path
                self.check_interval = check_interval
                self._last_successful_read = False

            def get_pause_status(self) -> bool:
                max_retries = 3
                retry_delay = 0.01

                for attempt in range(max_retries):
                    try:
                        with open(self.status_path, 'r', encoding='utf-8') as f:
                            status = f.read().strip()
                            self._last_successful_read = status == '1'
                            if attempt > 0:
                                print(
                                    f"【暂停控制器】第 {attempt + 1} 次尝试读取成功，状态={status}")
                            return self._last_successful_read
                    except (IOError, PermissionError) as e:
                        if attempt < max_retries - 1:
                            time.sleep(retry_delay)
                            continue
                        else:
                            print(
                                f"【暂停控制器】读取失败: {e}，使用上次值: {self._last_successful_read}")
                            return self._last_successful_read
                    except Exception as e:
                        print(f"【暂停控制器】读取失败: {e}，默认运行")
                        return False
                return False

            async def wait_if_paused(self):
                if not self.get_pause_status():
                    return

                print("【暂停控制器】检测到暂停状态，等待恢复...")
                start_time = asyncio.get_event_loop().time()
                timeout = 300
                check_count = 0

                while True:
                    if not self.get_pause_status():
                        print("【暂停控制器】状态已恢复为运行")
                        break

                    if asyncio.get_event_loop().time() - start_time > timeout:
                        try:
                            with open(self.status_path, 'w') as f:
                                f.write('0')
                        except Exception as e:
                            print(f"【暂停控制器】强制写入失败: {e}")
                        break

                    if check_count % 30 == 0:
                        elapsed = int(
                            asyncio.get_event_loop().time() - start_time)
                        print(f"【暂停控制器】已等待 {elapsed} 秒...")

                    await asyncio.sleep(self.check_interval)
                    check_count += 1

        # ---------------------- 策略运行器 ----------------------
        class StrategyRunner:
            def __init__(self, cycle_interval: float = 1.0, max_cycles: int = -1, snames: list = None, sids: list = None):
                self.cycle_interval = cycle_interval
                self.max_cycles = max_cycles
                self.snames = snames
                self.sids = sids  # 策略sid列表
                self.current_cycle = 0
                self.is_running = True
                self.num_strategies = 0
                self.strategy_queues = []
                self.ready_flags = []
                self.data_ready_flags = []  # 数据入队标志
                self.error_occurred = False
                self.pause_controller = FilePauseController(pause_status_dir)

            # ========== 关键2：仅入队，保持原始数据格式 ==========
            def _push_strategy_data(self, strategy_id: int, source, trade, info):
                """仅将单个策略数据入队（保持_update_datas返回的原始格式）"""
                # 行情数据入队（格式：[sid, source_list]）
                if source and not datas_queues[strategy_id].full():
                    datas_queues[strategy_id].put(source)
                # 交易数据入队（格式：[sid, trade_list]）
                if trade and not trade_queues[strategy_id].full():
                    trade_queues[strategy_id].put(trade)
                # 账户数据入队（格式：list[str]）
                if info and not account_queues[strategy_id].full():
                    account_queues[strategy_id].put(info)

                # 标记该策略当前周期数据已入队
                self.data_ready_flags[strategy_id] = True

            def _aggregate_and_write_all_data(self):
                """所有策略数据入队后，按live_plot要求的格式聚合写入（列表格式）"""

                # 校验所有策略数据是否都已入队
                if not all(self.data_ready_flags):
                    return

                # 1. 聚合行情数据：列表格式 [[sid0, source0], [sid1, source1], str]
                all_update_datas = []
                for i in range(self.num_strategies):
                    sid_i = self.sids[i]
                    if datas_queues[i].qsize() > 0:
                        source_data = datas_queues[i].get()
                        if isinstance(source_data, list) and len(source_data) == 2 and source_data[0] == sid_i:
                            all_update_datas.append(source_data)

                # 2. 聚合交易数据：列表格式 [[sid0, trade0], [sid1, trade1], str]
                all_update_trades = []
                for i in range(self.num_strategies):
                    sid_i = self.sids[i]
                    if trade_queues[i].qsize() > 0:
                        trade_data = trade_queues[i].get()
                        # 校验格式：必须是 [sid, trade_list]
                        if isinstance(trade_data, list) and len(trade_data) == 2 and trade_data[0] == sid_i:
                            all_update_trades.append(trade_data)

                # 3. 聚合账户数据：所有策略的账户信息（list[str]）
                all_account_info = []
                for i in range(self.num_strategies):
                    sname_i = self.snames[i]
                    if account_queues[i].qsize() > 0:
                        account_data = account_queues[i].get()
                        if isinstance(account_data, str):
                            all_account_info.append(
                                f"【策略 {sname_i}】{account_data}")

                # 4. 按live_plot要求的格式写入文件
                storeData(all_update_datas and all_update_datas or [],
                          update_datas_dir)
                storeData(
                    all_update_trades and all_update_trades or [], update_trade_dir)
                storeData(all_account_info and all_account_info or [],
                          account_info_dir)

                # 5. 重置数据就绪标志（为下一个周期准备）
                self.data_ready_flags = [False] * self.num_strategies

            async def wait_strategy_ready(self, strategy_id: int):
                if self.error_occurred or not self.is_running:
                    return

                self.ready_flags[strategy_id] = True

                # 所有策略执行完成后，先聚合写入数据，再推进下周期
                if all(self.ready_flags):
                    # 核心：按live_plot格式聚合写入
                    self._aggregate_and_write_all_data()

                    # 检查最大周期
                    if self.max_cycles != -1 and self.current_cycle >= self.max_cycles - 1:
                        print(
                            f"【{self.snames[strategy_id]}】已完成所有 {self.max_cycles} 个周期，准备退出")
                        self.is_running = False
                        for i in range(self.num_strategies):
                            await self.strategy_queues[i].put({"type": "exit"})
                        return

                    # 重置执行就绪标志
                    self.ready_flags = [False] * self.num_strategies
                    self.current_cycle += 1

                    # 发送下周期消息
                    for i in range(self.num_strategies):
                        try:
                            await asyncio.wait_for(
                                self.strategy_queues[i].put(
                                    {"type": "next_cycle"}),
                                timeout=5.0
                            )
                        except asyncio.TimeoutError:
                            self.error_occurred = True
                        except Exception as e:
                            self.error_occurred = True

                    # 周期间隔sleep
                    await asyncio.sleep(self.cycle_interval)

            def register_strategy(self, strategy_id: int):
                return self.StrategyNotifier(self, strategy_id, self.snames[strategy_id])

            class StrategyNotifier:
                def __init__(self, runner, strategy_id: int, sname: str):
                    self.runner = runner
                    self.strategy_id = strategy_id
                    self.sname = sname

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *args):
                    pass

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    try:
                        item = await asyncio.wait_for(
                            self.runner.strategy_queues[self.strategy_id].get(
                            ),
                            timeout=10.0
                        )
                        if item["type"] == "exit":
                            raise StopAsyncIteration
                        return item
                    except asyncio.TimeoutError:
                        # print(f"【Notifier】策略 {self.sname} 等待消息超时（10秒）")
                        if not self.runner.is_running or self.runner.error_occurred:
                            raise StopAsyncIteration
                        else:
                            # print(f"【Notifier】策略 {self.sname} 重试获取消息")
                            return await self.__anext__()
                    except Exception as e:
                        print(f"【Notifier】策略 {self.sname} 获取消息失败: {e}")
                        raise StopAsyncIteration

            async def run(self, strategies: list[Strategy]):
                self.num_strategies = len(strategies)
                self.strategy_queues = [asyncio.Queue()
                                        for _ in range(self.num_strategies)]
                self.ready_flags = [False] * self.num_strategies
                self.data_ready_flags = [False] * \
                    self.num_strategies  # 初始化数据就绪标志
                print(
                    f"【运行器】初始化完成 - 策略数={self.num_strategies}，队列数={len(self.strategy_queues)}")

                try:
                    # 创建策略任务
                    tasks = []
                    for idx, strategy in enumerate(strategies):
                        task = asyncio.create_task(
                            self._run_strategy(idx, strategy))
                        tasks.append(task)
                        print(f"【运行器】创建策略 {self.snames[idx]}")  # 任务: {task}")

                    # 启动第一个周期
                    print("\n===== 开始执行策略 =====")
                    for i in range(self.num_strategies):
                        try:
                            await self.strategy_queues[i].put({"type": "next_cycle"})
                            # print(f"【运行器】向策略 {i}（{self.snames[i]}）发送初始周期消息")
                        except Exception as e:
                            print(
                                f"【运行器】向策略 {i}（{self.snames[i]}）发送初始消息失败: {e}")

                    await asyncio.gather(*tasks)

                except Exception as e:
                    print(f"【运行器】执行出错: {str(e)}")
                    traceback.print_exc()
                finally:
                    print("\n===== 所有策略已退出，程序结束 =====")

            async def _run_strategy(self, strategy_id: int, strategy: Strategy):
                sname = self.snames[strategy_id]

                try:
                    async with self.register_strategy(strategy_id) as notifier:
                        cycle_count = 0
                        async for msg in notifier:

                            if not self.is_running or self.error_occurred:
                                print(f"【策略进程】策略 {sname} 退出条件满足，退出循环")
                                break

                            try:
                                # 检查暂停状态
                                await self.pause_controller.wait_if_paused()

                                # 检查索引是否超出范围
                                if strategy._btindex >= strategy._btklinedataset.max_length:
                                    print(
                                        f"【策略进程】策略 {sname} 索引已耗尽（{strategy._btindex}/{strategy._btklinedataset.max_length}），停止推进")
                                    self.is_running = False
                                    await self.strategy_queues[strategy_id].put({"type": "exit"})
                                    break

                                # 执行策略
                                strategy()  # __call__自动递增btindex

                                # 更新数据（保留原始格式：(source, trade, info)）
                                source, trade, info = strategy._update_replay_datas()
                                # 仅入队，不立即写入（保持原始格式）
                                self._push_strategy_data(
                                    strategy_id, source, trade, info)

                                # 通知运行器执行完成
                                await self.wait_strategy_ready(strategy_id)
                                cycle_count += 1

                            except Exception as e:
                                print(
                                    f"【策略进程】策略 {sname} 周期 {cycle_count} 执行出错: {str(e)}")
                                traceback.print_exc()
                                self.error_occurred = True
                                for i in range(self.num_strategies):
                                    await self.strategy_queues[i].put({"type": "exit"})
                                break

                except Exception as e:
                    print(f"【策略进程】策略 {sname} 框架出错: {str(e)}")
                    traceback.print_exc()
                    self.error_occurred = True
                finally:
                    print(f"【策略进程】策略 {sname} 已退出")

        # ---------------------- 主函数 ----------------------
        async def main(strategies: list[Strategy]):
            max_cycles = -1
            if strategies and hasattr(strategies[0], '_btklinedataset'):
                max_cycles_list = [
                    s._btklinedataset.max_length - s.min_start_length for s in strategies]
                max_cycles = max(max_cycles_list) if max_cycles_list else -1

            print(
                f"\n【主函数】初始化运行器 - 周期间隔: {cycle_interval}s, 最大周期: {max_cycles}")

            # 创建运行器（传入策略名称和sid）
            strategy_runner = StrategyRunner(
                cycle_interval=cycle_interval, max_cycles=max_cycles,
                snames=strategy_names, sids=strategy_sids)
            if hasattr(self, 'runner'):
                self.runner = strategy_runner

            # 后台启动画图进程（核心：去掉CREATE_NEW_CONSOLE，避免弹窗）
            try:
                cmd_list = [
                    'python', f"{BASE_DIR}/liveplot/live_plot.py",
                    '-bs', str(black_style),
                    '-pw', str(plot_width),
                    '-pm', str(period_milliseconds),
                    '-cp', click_policy
                    # 移除-multi参数，恢复原始格式兼容
                ]
                # Windows下用creationflags=0（无弹窗），Linux/Mac默认后台
                if os.name == 'nt':
                    # Windows后台运行+显示输出
                    proc = subprocess.Popen(
                        cmd_list,
                        # 移除stdout/stderr PIPE，恢复控制台输出
                        # stdout=subprocess.PIPE,
                        # stderr=subprocess.PIPE,
                        shell=False,
                        stdin=subprocess.DEVNULL,
                        creationflags=0  # 无新控制台窗口
                    )
                else:
                    # Linux/Mac后台运行+显示输出
                    proc = subprocess.Popen(
                        cmd_list,
                        shell=False,
                        stdin=subprocess.DEVNULL,
                        # 保留stderr输出到主控制台
                        stderr=sys.stderr,
                        stdout=sys.stdout
                    )
                await asyncio.sleep(2)
            except Exception as e:
                print(f"【主函数】启动画图进程失败: {e}")
                traceback.print_exc()

            # 运行策略
            try:
                # 设置【主函数】开始运行策略超时时间: 1800秒"
                await asyncio.wait_for(strategy_runner.run(strategies), timeout=1800)
            except asyncio.TimeoutError:
                print("【主函数】策略运行超时（30分钟），强制退出")
                strategy_runner.is_running = False
                for i in range(strategy_runner.num_strategies):
                    await strategy_runner.strategy_queues[i].put({"type": "exit"})

        # 顶层执行
        try:
            asyncio.run(main(self.strategies))
        except Exception as e:
            print(f"【顶层】主程序出错: {str(e)}")
            traceback.print_exc()
        finally:
            # 重置暂停状态
            try:
                with open(pause_status_dir, 'w') as f:
                    f.write('0')
                    f.flush()
                    os.fsync(f.fileno())
            except Exception as e:
                print(f"重置暂停状态失败: {e}")

    def _strategy_replay_light_chart(self, **kwargs):
        """回放模式 - light_chart分支：使用Qt内嵌图表界面进行策略回放

        与_strategy_replay的区别：
        - 不使用子进程 + 文件IPC通信
        - 直接在同一个Qt进程中内嵌图表
        - 使用SegmentedWidget进行多层导航
        - 带暂停/继续和速度控制
        """
        # 使用策略的 min_start_length（预热K线数），最小100
        default_candles = max(getattr(self.strategies[0], 'min_start_length', 300), 100)
        initial_candles = kwargs.pop('initial_candles', default_candles)
        replay_speed_ms = kwargs.pop('replay_speed_ms', 500)

        # 设置回放模式状态（回测已在run()中完成）
        for s in self.strategies:
            s._strategy_replay = True
            # 确保_btindex从初始位置开始
            if not hasattr(s, '_btindex') or s._btindex is None:
                s._btindex = initial_candles - 1
            else:
                s._btindex = max(s._btindex, initial_candles - 1)

        # 启动light_chart回放GUI
        from .strategy.light_chart_replay import main as replay_main
        replay_main(
            self.strategies,
            initial_candles=initial_candles,
            replay_speed_ms=replay_speed_ms,
            backtest_completed=True,  # 回测已完成，跳过重复回测
        )

    def _strategy_light_chart_display(self, **kwargs):
        """图表展示模式 - 回测完成后用Qt light_chart展示图表（不进行逐K线回放）

        与_strategy_replay_light_chart的区别：
        - 不显示ReplayInfoWindow（暂停/继续/速度控件）
        - 一次性加载全部K线数据和指标，不进行逐K线更新
        - 仅用于回测后的图表浏览和查看
        """
        # 使用策略的 min_start_length（预热K线数），最小100
        default_candles = max(getattr(self.strategies[0], 'min_start_length', 300), 100)
        initial_candles = kwargs.pop('initial_candles', default_candles)
        replay_speed_ms = kwargs.pop('replay_speed_ms', 500)

        # 设置策略状态
        for s in self.strategies:
            #s._strategy_replay = True
            if not hasattr(s, '_btindex') or s._btindex is None:
                s._btindex = initial_candles - 1
            else:
                s._btindex = max(s._btindex, initial_candles - 1)

        # 启动light_chart显示GUI（display_only模式）
        from .strategy.light_chart_replay import main as replay_main
        replay_main(
            self.strategies,
            initial_candles=initial_candles,
            replay_speed_ms=replay_speed_ms,
            backtest_completed=True,  # 回测已完成
            display_only=True,  # 纯图表展示模式（不逐K线回放）
        )

    def optstrategy(self, target: OPTTargetType = 'profit_ratio',
                    weights: float | tuple[float] = 1., opconfig: GAOpConfig | OptunaConfig | dict | list[dict] = {}, op_method: OPTMethodType = 'optuna', show_bar=True, skip=False, **kwargs):
        """## 策略参数优化配置（设置优化目标、方法、参数，不实际执行优化）

        ### 📘 **文档参考**:
        - https://www.minibt.cn/minibt_basic/1.5minibt_strategy_param_optimization/

        Note:
            kwargs 参数格式说明：
                - 数值范围（带步长）：range(1,10)（1到10步长1）、(10,30,2)（10到30步长2）
                - 固定选项：[3,5,8,13]（仅从列表中选择参数值）
                - 固定值：10（参数不参与优化，固定为该值）

        Args:
            target (str, optional): 优化目标（QuantStats性能指标），默认'profit_ratio'（盈利比）
                                    支持指标列表见函数内注释，部分指标需额外传参（如'compare'需传'benchmark'）
            weights (float | tuple[float], optional): 优化目标权重（默认1.0）
                                                     - 正数：最大化目标（如1.0表示最大化盈利比）
                                                     - 负数：最小化目标（如-1.0表示最小化最大回撤）
                                                     - 元组：多目标优化，权重与target一一对应
            opconfig (GAOpConfig | dict, optional): 优化配置参数（默认空dict）
                                                  - GA优化：传OpConfig实例或dict
                                                  - Optuna优化：传OptunaConfig实例或dict
            op_method (Literal['ga', 'optuna'], optional): 优化方法（默认'ga'）
                                                           - 'ga'：遗传算法（基于DEAP库）
                                                           - 'optuna'：贝叶斯优化（基于Optuna库）
            show_bar (bool, optional): 是否显示优化进度条（默认True）
            skip: (bool): True/False（是否跳过优化，默认False）
            kwargs: 待优化参数（格式见Note）
                path (str): 参数保存路径. 默认'./minibt/op_params/'
        """
        # 跳过优化（通过skip参数控制，用于条件执行）
        if skip:
            return self

        # 保存优化配置（供后续run()方法调用时使用）
        self.__target = target          # 优化目标
        self.__weights = weights        # 目标权重
        self.__opconfig = opconfig      # 优化配置
        self.__op_method = op_method    # 优化方法
        self.__op_show_bar = show_bar    # 进度条开关
        self.__isoptimize = True        # 标记开启优化模式

        # 配置优化结果保存路径（默认./minibt/op_params/）
        from .utils import BASE_DIR
        self.__op_path = kwargs.pop(
            'path', os.path.join(BASE_DIR, "op_params"))
        self.__op_kwargs = kwargs        # 待优化参数列表

        return self

    def __optstrategy(self):
        """私有方法：执行遗传算法（GA）优化策略参数（基于DEAP库）"""
        # 导入GA优化所需库和类
        from deap import creator, base  # DEAP核心：创建适应度和个体类
        from .strategy.optimize import GAOptimizer  # 自定义GA优化器

        # 读取优化配置（从optstrategy()方法保存的属性中获取）
        target, weights, opconfig, kwargs = self.__target, self.__weights, self.__opconfig, self.__op_kwargs

        # 断言：必须传入待优化参数（否则无法进行优化）
        assert isinstance(kwargs, dict) and kwargs, '请设置优化参数（在kwargs中指定）'
        print(f"优化参数为：{kwargs}")  # 打印待优化参数，便于调试

        # 处理优化目标：确保为列表/元组格式（统一多目标处理逻辑）
        if target and isinstance(target, str):
            target = [target,]  # 单目标转列表
        # 断言：目标类型必须是列表/元组，且元素为字符串（QuantStats指标名）
        assert isinstance(target, (list, tuple)), 'target为字符串、列表或元组'
        assert [isinstance(x, str)
                for x in target], 'target元组元素非字符串（必须是QuantStats指标名）'

        # 初始化GA优化器（仅支持单策略优化，取第一个策略和第一个数据集）
        strategy, datas = self.strategies[0], self.__datas[0]
        # 若未传入优化配置，使用默认OpConfig
        if not (isinstance(opconfig, dict) and opconfig):
            from .utils import GAOpConfig
            opconfig = GAOpConfig()
        op = GAOptimizer(strategy, datas, target, **opconfig)  # 实例化GA优化器

        # 解析策略参数（获取策略默认参数及键名）
        params = strategy.params
        params_keys = list(params.keys())

        # 遍历待优化参数，添加到GA优化器（区分可变参数/固定参数）
        for key, value in kwargs.items():
            # 仅处理策略已定义的参数（忽略无关参数）
            if key in params_keys:
                # 处理可迭代参数（参与优化的参数，如range、tuple、list）
                if isinstance(value, Iterable):
                    # range类型：转成[start, stop, step]列表
                    if isinstance(value, range):
                        _value = [value.start, value.stop, value.step]
                    # tuple类型：必须是( start, stop, step )格式（3个数值元素）
                    elif isinstance(value, tuple):
                        assert len(
                            value) == 3, '参数个数不足（tuple需为3个元素：start, stop, step）'
                        assert all([isinstance(x, (float, int))
                                   for x in value]), 'tuple元素非数字'
                        _value = value
                    # list类型：固定选项（仅从列表中选择参数值，不进行范围搜索）
                    elif isinstance(value, list):
                        assert value or all(
                            [isinstance(x, (float, int)) for x in value]), 'list元素非数字'
                        op.add_listed_param(key, value)  # 添加固定选项参数
                        continue
                    # 其他可迭代类型：不支持，抛出异常
                    else:
                        raise Exception('参数有误（可迭代参数仅支持range/tuple/list）')
                    # 添加可变参数（范围搜索）
                    op.add_mutable_param(key, *_value)
                    continue
                # 非可迭代参数：固定值（不参与优化）
                op.add_fixed_param(key, value)

        # 补充策略默认参数（未在kwargs中指定的参数，按默认值固定）
        for k, v in params.items():
            if k not in op.mutable_params:  # 仅补充非可变参数
                op.add_fixed_param(k, v)

        # 配置GA适应度函数（单目标/多目标）
        # 1. 单目标优化（权重为单个数值）
        if isinstance(weights, (float, int)):
            assert weights, '权重不能为0（无法判断优化方向）'
            if weights > 0.:  # 正权重：最大化目标（如最大化盈利比）
                weights = (1.,)
                name = "FitnessMax"  # 适应度类名（最大化）
            else:  # 负权重：最小化目标（如最小化最大回撤）
                weights = (-1.,)
                name = "FitnessMin"  # 适应度类名（最小化）
        # 2. 多目标优化（权重为列表/元组）
        else:
            assert isinstance(weights, (list, tuple)
                              ), 'weights为float | tuple[float]（多目标需传元组）'
            assert [isinstance(x, (int, float))
                    for x in weights], 'weights元组元素非数字'
            weights = tuple(weights)
            name = "FitnessCompound"  # 多目标适应度类名

        # 创建DEAP适应度类和个体类
        creator.create(name, base.Fitness, weights=weights)  # 适应度类（关联权重）
        creator.create("Individual", list, fitness=getattr(
            creator, name))  # 个体类（继承list，关联适应度）

        # 启动GA优化
        op.go(weights)
        self.__is_finish = True  # 标记优化完成

    def __optuna(self, strategy_: Strategy, isplot: bool = True) -> dict:
        """私有方法：执行Optuna贝叶斯优化策略参数（支持单目标/多目标）

        Args:
            strategy_: 待优化的策略类（Strategy子类）
            isplot: 是否生成优化结果可视化图表（默认True）

        Returns:
            dict: 最优参数组合（Optuna最佳试验的params）
        """
        import optuna
        target, weights, config, kwargs = self.__target, self.__weights, self.__opconfig, self.__op_kwargs

        # 1. 校验待优化参数（必须为非空字典）
        if not (isinstance(kwargs, dict) and kwargs):
            raise ValueError("请设置有效的优化参数（非空字典）")
        print(f"优化参数为：{kwargs}")  # 打印优化参数，便于调试

        # 2. 处理优化目标（确保为列表/元组，统一多目标逻辑）
        if isinstance(target, str):
            target = [target]  # 单目标转列表
        if not isinstance(target, (list, tuple)):
            raise TypeError("target必须为列表或元组（元素为QuantStats指标名）")
        if not all(isinstance(x, str) for x in target):
            raise TypeError("target的元素必须是字符串（QuantStats指标名）")
        num_target = len(target)

        # 3. 处理Optuna配置（拆分优化参数和研究参数）
        optimize_kwargs: dict = {}  # 优化参数（如n_trials、n_jobs）
        study_kwargs: dict = {}     # 研究参数（如sampler、pruner）
        if config and isinstance(config, tuple) and len(config) == 2:
            optimize_kwargs, study_kwargs = config  # 解包用户配置
        else:
            from .utils import OptunaConfig
            optimize_kwargs, study_kwargs = OptunaConfig()

        # 4. 配置优化运行参数
        optimize_kwargs['show_progress_bar'] = self.__op_show_bar  # 进度条开关
        if optimize_kwargs.get('n_jobs') == 'max':
            optimize_kwargs['n_jobs'] = MAX_WORKERS  # 并行线程数

        # 5. 初始化优化用策略（创建最优策略类，标记为优化模式）
        self.strategies = [strategy_.copy(
            name=f"best_trial{strategy_.__name__}"),]
        name = strategy_.__name__
        strategy: Strategy = strategy_(_isoptimize=True)._start_strategy_run()
        # 关闭闲置TqApi连接（避免资源泄漏）
        if hasattr(strategy._api, "close"):
            strategy._api.close()
        # RL策略特殊处理：加载Actor模型
        if strategy.rl:
            ...

        # --------------------------
        # 6. 解析待优化参数（核心修改：区分int/float类型）
        # --------------------------
        params = strategy.params  # 策略默认参数
        params_keys = list(params.keys())
        trial_params: dict[str, list] = {}  # 采样配置（Optuna用）
        i = 0  # 参数索引（区分同类型参数：如int0、float1）

        for key, value in kwargs.items():
            if key not in params_keys:
                continue  # 仅处理策略已定义的参数

            # 处理可迭代参数（range/tuple/list，排除字符串）
            if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
                _value = None  # 解析后的参数范围（start, stop, step）

                # 处理range类型（转成[start, stop, step]）
                if isinstance(value, range):
                    _value = [value.start, value.stop, value.step]
                # 处理tuple类型（至少2个元素：start, stop，step可选）
                elif isinstance(value, tuple):
                    assert len(
                        value) >= 2, f'参数{key}的tuple需至少2个元素（start, stop），当前{len(value)}个'
                    assert all(isinstance(x, (float, int))
                               for x in value), f'参数{key}的tuple元素必须是数字'
                    _value = list(value)
                # 处理list类型（固定选项：分类参数）
                elif isinstance(value, list):
                    valid_types = (float, int, bool, str)
                    assert all(isinstance(x, valid_types)
                               for x in value), f'参数{key}的list元素必须是{valid_types}类型'
                    trial_params[f"categorical{i}"] = [key, value]  # 分类参数配置
                    i += 1
                    continue

                # 处理数值型参数（int/float）
                if _value:
                    # 1. 校验范围有效性（start < stop）
                    assert _value[0] < _value[1], f'参数{key}范围无效（start={_value[0]} >= stop={_value[1]}）'
                    # 2. 确定参数类型（只要有1个float元素，就视为float类型）
                    if any(isinstance(x, float) for x in _value):
                        param_type_name = 'float'
                    else:
                        param_type_name = 'int'
                    # 3. 补充默认step（int默认1，float默认None）
                    if len(_value) < 3:
                        _value.append(1 if param_type_name == 'int' else None)
                    # 4. 校验step有效性（正数，int的step必须是int）
                    step = _value[2]
                    if step is not None:
                        assert step > 0, f'参数{key}的step必须为正数，当前{step}'
                        if param_type_name == 'int':
                            assert isinstance(
                                step, int), f'参数{key}是int类型，step必须是int，当前{type(step)}'
                    # 5. 保存采样配置（key: 类型+索引，value: [参数名, start, stop, step]）
                    trial_params[f"{param_type_name}{i}"] = [key] + _value
                    i += 1
            else:
                # 非可迭代参数（固定值，无需采样）
                params[key] = value

        # 7. 配置Optuna研究方向（单目标/多目标）
        if isinstance(weights, (float, int)):
            assert weights, '权重不能为0（无法判断优化方向）'
            study_kwargs['direction'] = "maximize" if weights >= 0. else "minimize"
            ismax = True
        else:
            assert isinstance(weights, (list, tuple)
                              ), 'weights为float | tuple[float]（多目标需传元组）'
            assert [isinstance(x, (int, float))
                    and x for x in weights], 'weights元组元素非数字或为0'
            study_kwargs['directions'] = ["maximize" if x >
                                          0. else "minimize" for x in weights]
            ismax = study_kwargs['directions'][0] == "maximize"

        # --------------------------
        # 8. 定义Optuna采样函数
        # --------------------------
        def get_params(trial):
            # 基于默认参数更新为当前trial的采样值
            for k, v in trial_params.items():
                # 提取采样方法类型（如int0→int、float1→float、categorical2→categorical）
                k_clean = ''.join([x for x in k if not x.isdigit()])
                # 获取Optuna对应的采样方法
                suggest_method = getattr(trial, f'suggest_{k_clean}')

                # 分类型处理采样逻辑
                if k_clean == 'categorical':
                    # 分类参数：v = [参数名, 选项列表]
                    param_name, choices = v[0], v[1]
                    params[param_name] = suggest_method(
                        name=param_name, choices=choices)
                else:
                    # 数值参数：v = [参数名, start(low), stop(high), step]
                    param_name, low, high, step = v[0], v[1], v[2], v[3]
                    # 调用采样方法（step为None时不传递，避免Optuna报错）
                    if step is None:
                        params[param_name] = suggest_method(
                            name=param_name, low=low, high=high)
                    else:
                        params[param_name] = suggest_method(
                            name=param_name, low=low, high=high, step=step)
            return Addict(params)  # 支持属性式访问（如params.length）

        def objective(trial: optuna.Trial):
            # try:
            result = strategy(get_params(trial), ismax, target)
            # except:
            #     result = tuple([0.]*num_target)
            return result

        # 9. 初始化Optuna研究（配置采样器、剪枝器）
        for k, v in study_kwargs.items():
            if k in ['pruner', 'sampler'] and isinstance(v, str):
                # 字符串转Optuna类实例（如'samplers.TPESampler'→TPESampler()）
                module = getattr(optuna, ''.join([k, 's']))
                study_kwargs[k] = getattr(module, v)()

        # 抑制Optuna冗余日志
        if not study_kwargs.pop('logging', True):
            optuna.logging.set_verbosity(optuna.logging.WARNING)
            optuna.logging.disable_default_handler()
            optuna.logging.disable_propagation()

        # 创建Optuna研究
        optunaplot = study_kwargs.pop('optunaplot', None)
        study: optuna.Study = optuna.create_study(**study_kwargs)

        # 10. 启动Optuna优化
        study.optimize(objective, **optimize_kwargs)

        # 11. 处理优化结果
        trials = sorted(study.best_trials, key=lambda t: t.values)
        assert trials, "无优化结果（未生成有效试验）"
        best_trial = trials[-1]  # 最优试验（最后一个为最优）

        # 打印最优结果
        print("Number of finished trials: ", len(study.trials))
        print("Best trial:")
        print("  Value: ", dict(zip(target, best_trial.values)))
        print("  Params: ", best_trial.params)

        # 保存优化结果到CSV
        df: pd.DataFrame = study.trials_dataframe(
            attrs=('number', 'value', 'params'))
        value_cols = [col for col in df.columns if 'value' in col]
        df.sort_values(by=value_cols, ignore_index=True,
                       inplace=True, ascending=False)
        os.makedirs(self.__op_path, exist_ok=True)
        df.to_csv(os.path.join(self.__op_path,
                  f'opt_{name}_{target[0]}.csv'), index=False)
        print(df.head(10))  # 打印前10个最优试验

       # 13. 优化结果可视化：按「单/多目标」动态适配图表和参数
        if isplot:
            # 根据目标数量选择可视化图表
            if len(target) == 1:
                # 单目标优化：支持的图表（选择其一）
                # 可选："plot_optimization_history"（优化历史）、"plot_param_importances"（参数重要性）
                optunaplot = "plot_optimization_history"  # 推荐优先使用这个

                # 为不同图表单独配置参数（避免传递不支持的参数）
                if optunaplot == "plot_optimization_history":
                    # plot_optimization_history仅支持target参数（可选）
                    plot_kwargs = {
                        "target": lambda t: t.values[0]  # 只关注第一个目标值
                    }
                elif optunaplot == "plot_param_importances":
                    # plot_param_importances需要params和target参数
                    plot_kwargs = {
                        "params": params_keys,  # 策略参数列表
                        "target": lambda t: t.values[0]
                    }
                else:
                    plot_kwargs = {}  # 其他图表默认无参数
                plot_func = getattr(optuna.visualization, optunaplot)
                plot_func(study, **plot_kwargs).show()
            else:
                # 多目标优化：仅使用帕累托前沿图
                optunaplot = "plot_pareto_front"
                # 动态传递所有目标值（不硬编码索引）
                # plot_kwargs = {
                #     "targets": lambda t: tuple(t.values),  # 适配任意数量的目标
                #     "target_names": target  # 显示实际目标名称（如["profit", "sharpe"]）
                # }
                plot_kwargs = dict(
                    plot_rank=dict(params=params_keys,
                                   target=lambda t: t.values[0]),  # 参数排名图
                    plot_pareto_front=dict(  # 帕累托前沿图（多目标）
                        targets=lambda t: (t.values[0], t.values[1]),
                        target_names=["Objective 0", "Objective 1"]
                    ),
                    plot_param_importances=dict(  # 参数重要性图
                        target=lambda t: t.values[0], params=params_keys
                    )
                )
                getattr(optuna.visualization, optunaplot)(
                    study, **plot_kwargs.get(optunaplot)).show()

            # 从plot_params中获取当前图表对应的参数（避免参数不匹配）
        # 14. 初始化策略实例管理（重置全局策略实例）
        from .utils import StrategyInstances
        Base._strategy_instances = StrategyInstances()
        # 更新最优策略的参数（供后续回测使用）
        self.strategies[-1].params = Addict(best_trial.params)
        self.strategies[-1]._profit_plot = True

        return best_trial.params  # 返回最优参数

    def run(self, isplot=True, isreport: bool = False, **kwargs) -> Bt:
        """## 策略执行入口函数（根据配置自动识别运行模式：实盘交易/参数优化/回测分析）

        ### 本方法根据初始化配置自动选择运行模式：
        - 实盘交易模式 (live)
        - 参数优化模式 (optimize) 
        - 回测分析模式 (backtest)
        - 策略回放模式 (replay)

        支持多策略并行回测及参数优化，完成后可生成可视化图表和分析报告。

        Args:
            isplot (bool, optional): 是否生成可视化图表（Bokeh）。默认为 True
            isreport (bool, optional): 是否生成QuantStats量化分析报告。默认为 False

        Kwargs:
            #### 回放模式参数
            - replay (bool): 是否启用策略回放模式。默认为 False（也可在Bt(replay=True)中设置）
            - gui (str): 回放GUI类型，可选['live_plot', 'light_chart']。默认为 'live_plot'
                - 'live_plot': 文件IPC模式，启动独立画图进程
                - 'light_chart': Qt内嵌图表模式，使用SegmentedWidget导航（light_chart_replay）
            - initial_candles (int): light_chart模式初始加载K线数。默认300
            - replay_speed_ms (int): light_chart模式回放速度(ms/根K线)。默认500
            - update_length (int): live_plot模式更新数据长度。默认10

        Kwargs:
            #### 多策略并行参数
            - model (str): 并行计算库选择，可选 ['sequential','dask','joblib','sklearn','multiprocessing']。
                - 'sequential': 顺序执行（默认，推荐！避免 TqApi 数据竞态导致多策略结果不一致）
                - 'joblib': 线程并行（多策略共享 TqApi 时可能数据竞态）

            #### 可视化图表参数 (传递至 bokeh_plot)
            - trade_signal (bool): 是否显示交易信号标记（开仓/平仓点）。默认为 True
            - black_style (bool): 是否使用黑色主题风格（默认为白色主题）。默认为 False
            - plot_width (int): 图表显示宽度（像素值，默认全屏自适应）
            - plot_cwd (str): 图表文件存储目录路径。默认为当前工作目录
            - plot_name (str): 图表文件名称。默认为 ''
            - open_browser (bool): 是否在浏览器中自动打开图表（Jupyter中建议关闭）。默认为 False
            - save_plot (bool): 是否保存图表HTML文件。默认为 True

            #### 分析报告参数 (传递至 qs_reports)
            - report_cwd (str): 报告存储目录路径。默认为当前工作目录
            - report_name (str): 报告文件名称（默认自动生成）
            - report_height (int): 报告页面显示高度。默认为 800
            - show (bool): 是否在生成后立即显示报告。默认为 True
            - keep_temp (bool): 是否保留临时计算文件。默认为 True

            #### 可统一设置图表及报告文件存储目录路径及名称
            - cwd (str): 图表及报告文件存储目录路径。默认为当前工作目录
            - name (str): 图表及报告文件名称。默认为 ''

            #### 实盘交易参数
            - period_milliseconds (int): 实盘数据更新频率（毫秒）。默认为 0（实时更新）
            - update_length (int): 实盘和回收中更新数据的长度，若网络有延时，可设置更长的更新长度。默认为 10

            #### 账户信息参数
            - print_account (bool): 是否打印账户详细信息。默认为 False

        Returns:
            Bt: 返回当前Bt实例，支持链式调用

        Raises:
            AssertionError: 当未添加策略或实盘模式未初始化天勤API时抛出
        """
        # 支持通过 run() 参数覆盖构造函数中的 replay 设置
        replay = kwargs.pop('replay', self.__replay)
        # 1. 自动加载策略（若未手动添加策略）
        if not self.strategies:
            from .strategy.strategy import default_strategy  # 导入默认策略
            # 查找所有Strategy子类（包括子类的子类，即自定义策略）
            strategy_list = Strategy.__subclasses__()
            strategy_list += list(flatten([s.__subclasses__()
                                  for s in self.instances]))

            # 扩展策略列表（包含更深层次的子类）
            if strategy_list:
                _sl = [s.__subclasses__() for s in strategy_list]
                strategy_list += list(flatten(_sl))
                # 排除默认策略（优先使用自定义策略）
                if default_strategy in strategy_list:
                    strategy_list.pop(strategy_list.index(default_strategy))
                # 添加找到的自定义策略
                if strategy_list:
                    for s in strategy_list:
                        self.addstrategy(s)

            # 若仍无策略，添加默认策略
            if not self.strategies:
                print("无策略，已添加默认策略！")
                self.addstrategy(default_strategy)

        # 2. 校验策略数量（必须至少有一个策略）
        num_strategy = len(self.strategies)
        assert num_strategy > 0, '请添加策略（通过addstrategy()或开启auto=True自动加载）'

        # 4. 全局资源注入（供策略内部访问）
        if self.__datas:
            Base._datas = self.__datas  # 注入回测数据
        if self._api:
            Base._api = self._api        # 注入TqApi实例

        # 5. 传递额外参数到所有策略（如画图开关、账户配置）
        for k, v in kwargs.items():
            [setattr(strategy, k, v) for strategy in self.strategies]

        # 5.6 自动检测策略 op_config 优化配置（无需手动调用 optstrategy()）
        if not self.__isoptimize:
            for s_cls in self.strategies:
                op_cfg = getattr(s_cls, 'op_config', None)
                if op_cfg and isinstance(op_cfg, dict) and op_cfg.get('params'):
                    self.optstrategy(
                        target=op_cfg.get('target', 'profit_ratio'),
                        weights=op_cfg.get('weights', 1.),
                        opconfig=op_cfg.get('opconfig', {}),
                        op_method=op_cfg.get('op_method', 'optuna'),
                        show_bar=op_cfg.get('show_bar', True),
                        skip=op_cfg.get('skip', False),
                        **op_cfg.get('params', {}),
                    )
                    # 只取第一个有 op_config 的策略
                    # 参数优化只支持单策略
                    break  

        # 6. 分支1：实盘模式（__live=True时）
        if self.__live:
            assert self._api, "请连接天勤api（先调用addTqapi()初始化）"
            Base._is_live_trading = True  # 标记为实盘模式（供策略内部判断）
            self.__tq_real(isplot, **kwargs)  # 执行实盘逻辑
            return self

        # 7. 分支2：参数优化模式（__isoptimize=True时）
        elif self.__isoptimize:
            # 清空全局策略实例集合，避免上次回测残留干扰优化
            from .utils import StrategyInstances
            Base._strategy_instances = StrategyInstances()
            # 按优化方法执行（GA/Optuna）
            if self.__op_method == 'optuna':
                self.__optuna(self.strategies[0], isplot)  # Optuna优化（单策略）
            elif self.__op_method == 'ga':
                self.__optstrategy()  # GA优化（单策略）

        # 8. 分支3：回测模式（默认分支）
        # 8.0 清空全局策略实例集合，避免上次回测残留导致 _converted_key 查找错误
        from .utils import StrategyInstances
        Base._strategy_instances = StrategyInstances()

        # 8.1 初始化策略实例（为每个策略分配唯一ID）
        self.strategies = [s(_sid=i, _isoptimize=False, _isreplay=replay)
                           for i, s in enumerate(self.strategies)]

        # 8.2 单策略回测（含RL策略）
        if num_strategy <= 1:
            # 实例化策略并执行回测（调用策略__call__方法）
            self.strategies = [s() for s in self.strategies]
            # RL策略特殊处理：若开启随机策略测试，直接返回（不执行完整回测）
            if self.strategies[0].rl and self.strategies[0]._rl_config.random_policy_test:
                return self

        # 8.3 多策略回测（默认顺序执行，避免 TqApi 数据竞态；可通过 model='joblib' 切换为并行）
        else:
            # 读取并行库参数（默认 sequential 顺序执行）
            parallel_model = kwargs.pop('model', 'sequential')
            self.strategies = self.__multi_run(parallel_model)

        # 9. 回测完成后处理
        self.__is_finish = True  # 标记回测完成

        # 9.1 打印账户信息（若配置开启）
        print_account = kwargs.pop('print_account', False)
        # 遍历所有策略，满足条件则打印账户和策略信息
        [t.pprint if t.config.pprint else (t._print_account, t.richprint)  for t in self.strategies
            if t.config.print_account or print_account]
        if replay:  # 策略回放
            gui = kwargs.pop('gui', 'live_plot')
            if gui == 'light_chart':
                self._strategy_replay_light_chart(**kwargs)
            else:
                self._strategy_replay(**kwargs)
            return self
        else:
            # 9.2 生成图表（若isplot=True）
            if isplot:
                gui = kwargs.pop('gui', 'bokeh')
                if gui == 'light_chart':
                    # 使用light_chart Qt图表展示模式（display_only，不逐K线回放）
                    self._strategy_light_chart_display(**kwargs)
                else:
                    self.bokeh_plot(**kwargs)

        # 9.3 生成QuantStats分析报告（若isreport=True）
        if isreport:
            self.qs_reports(** kwargs)

        # 9.4 打印回测耗时（若策略配置开启计时）
        if self.strategies[0].config.take_time:
            elapsed_time = round(_time.time() - self.__start_time, 2)
            print(f"耗时：{elapsed_time}秒")

        # 9.5 关闭闲置资源（避免泄漏）
        if hasattr(self._api, 'close') and not self.strategies[0]._light_chart:
            self._api.close()  # 关闭TqApi连接

        if self.strategies[0].config.islog:
            self.strategies[0].logger.close_logger()

        return self

    def __multi_run(self, model: str) -> list[Strategy]:
        """私有方法：多策略并行回测（支持4种并行库，按需选择）

        Args:
            model (str): 并行库标识（'dask'/'joblib'/'sklearn'/'multiprocessing'）

        Returns:
            list[Strategy]: 已完成回测的策略实例列表
        """
        scheduler = 'threading'  # 并行调度器（线程模式，避免多进程数据拷贝）
        # 动态调整最大工作线程数（不超过MAX_WORKERS，避免资源耗尽）
        max_workers = min(MAX_WORKERS, len(self.strategies))

        # 按并行库类型执行多策略回测
        if model == 'dask':
            # Dask：适合分布式计算，支持复杂任务依赖
            from dask import delayed, compute
            futures = [delayed(s)() for s in self.strategies]  # 生成延迟任务
            results = list(compute(*futures, scheduler=scheduler))  # 执行任务并获取结果

        elif model == 'joblib':
            # Joblib：轻量级并行，适合简单循环任务（推荐）
            from joblib import Parallel, delayed
            results = list(
                Parallel(n_jobs=max_workers, backend=scheduler)(
                    delayed(s)() for s in self.strategies  # 每个策略作为一个任务
                )
            )

        elif model == 'sklearn':
            # Scikit-learn：适配sklearn生态，适合与机器学习流程结合
            from sklearn.utils import parallel_backend
            from joblib import Parallel, delayed
            # 配置并行后端（线程模式）
            with parallel_backend(scheduler):
                results = list(
                    Parallel(n_jobs=max_workers)(
                        delayed(s)() for s in self.strategies
                    )
                )

        elif model == 'sequential':
            # Sequential：顺序执行（避免 TqApi 数据竞态，推荐多策略默认用此模式）
            results = [s() for s in self.strategies]

        else:
            # 多进程（ProcessPoolExecutor）：适合CPU密集型任务
            from concurrent.futures import ProcessPoolExecutor, as_completed, wait, FIRST_COMPLETED
            # 初始化进程池
            executor = ProcessPoolExecutor(max_workers=max_workers)
            all_task = [executor.submit(s)
                        for s in self.strategies]  # 提交所有策略任务
            # 等待第一个任务完成（避免同时启动过多进程）
            wait(all_task, return_when=FIRST_COMPLETED)
            # 收集所有任务结果（按完成顺序）
            results: list[Strategy] = []
            for f in as_completed(all_task):
                result = f.result()
                results.append(result)
            executor.shutdown()  # 关闭进程池

        return results

    def bokeh_plot(
            self,
            trade_signal: bool = True,
            black_style: bool = False,
            open_browser: bool = False,
            plot_width: int = None,
            plot_cwd="",
            plot_name: str = "",
            save_plot: bool = False,
            **kwargs) -> Tabs:
        """## 生成Bokeh交互式可视化图表（展示回测结果分析）

        本方法基于回测结果数据生成交互式图表，包含以下核心组件：
        - 价格走势图表（K线/折线）
        - 技术指标图表（如均线、MACD等）
        - 交易信号标记（开仓/平仓点）
        - 账户净值曲线

        Args:
            trade_signal (bool, optional): 是否显示交易信号标记（开仓/平仓点）。默认为 True
            black_style (bool, optional): 是否使用黑色主题风格（默认为白色主题）。默认为 False
            open_browser (bool, optional): 是否在浏览器中自动打开图表（Jupyter中建议关闭）。默认为 False
            plot_width (int, optional): 图表显示宽度（像素值，默认全屏自适应）
            plot_cwd (str, optional): 图表文件存储目录路径。默认为当前工作目录
            plot_name (str, optional): 图表文件名称（不含后缀）。默认为 'bokeh_plot'

        Returns:
            Tabs: 返回bokeh.models.Tabs

        Raises:
            UserWarning: 当策略尚未完成回测时发出警告

        Note:
            - 需先调用run()方法完成回测后再调用此方法
            - 图表默认保存为HTML文件，可在浏览器中交互查看
            - 多策略回测时可选择显示特定策略的图表
        """
        # 导入Bokeh绘图函数（避免循环导入，延迟导入）
        from .strategy.bokeh_plot import plot
        # 校验：回测未完成时，打印警告并返回
        if not self.__is_finish:
            return self.strategies[0].logger.warning('策略尚未回测（请先调用run()执行回测）')

        # 调用绘图函数生成图表
        tabs = plot(self.strategies, trade_signal, black_style,
                    open_browser, plot_width, plot_cwd, plot_name, save_plot)

        return tabs

    def qs_reports(self,
                   report_cwd: str = "",
                   report_name: str = "",
                   report_height: int = 800,
                   show: bool = True,
                   keep_temp: bool = True,
                   **kwargs) -> Bt:
        """
        ## 生成QuantStats量化分析报告（支持多策略合并为单一HTML文件）

        本方法基于回测结果生成专业的QuantStats量化分析报告，包含以下核心分析模块：
        - 收益表现分析（年化收益、夏普比率、索提诺比率等）
        - 风险指标分析（最大回撤、波动率、VaR等）
        - 交易行为分析（胜率、盈亏比、持仓周期等）
        - 绩效归因分析（收益来源分解）
        - 可视化图表（收益曲线、回撤曲线、月度收益热力图等）

        支持多策略回测结果合并，生成统一的导航式报告页面。

        Args:
            report_cwd (str, optional): 报告存储目录路径。默认为当前工作目录
            report_name (str, optional): 报告文件名称（不含后缀）。默认为自动生成
            report_height (int, optional): 报告显示高度（像素值）。默认为 800
            show (bool, optional): 是否在生成后立即显示报告。默认为 True
            keep_temp (bool, optional): 是否保留临时生成的文件。默认为 False

        Returns:
            Bt: 返回当前Bt实例，支持链式调用

        Raises:
            ImportError: 当缺少必要依赖库时抛出
            PermissionError: 当无权限创建或写入目录时抛出
            ValueError: 当无有效报告可合并时抛出
            RuntimeError: 当保存合并报告失败时抛出

        Note:
            - 需先调用run()方法完成回测后再调用此方法
            - 多策略回测时会自动合并所有策略报告为单一HTML文件
            - 报告默认保存为HTML格式，可在浏览器中交互查看
            - Jupyter环境中会自动嵌入显示报告，非Jupyter环境会打开浏览器
        """
        if not hasattr(self,"strategies"):
            self.strategies = kwargs.pop("strategies",[])
        try:
            from IPython import get_ipython
            _shell = get_ipython()
            IS_JUPYTER_NOTEBOOK = _shell is not None and _shell.__class__.__name__ == 'ZMQInteractiveShell'
        except (ImportError, AttributeError):
            IS_JUPYTER_NOTEBOOK = False

        # 单策略直接使用原方法
        if len(self.strategies) == 1:
            self.strategies[0]._qs_reports(
                report_cwd, report_name, True, **kwargs)
            return self

        if not report_name:
            report_name = "merged"

        # -------------------------- 1. 前置校验 --------------------------
        if not self.__is_finish:
            self.strategies[0].logger.warning('策略尚未回测（请先调用run()执行回测）')
            return self

        try:
            from bs4 import BeautifulSoup
            import webbrowser
            import shutil
        except ImportError:
            raise ImportError(
                "请先安装webbrowser、beautifulsoup4和shutil：pip install webbrowser beautifulsoup4 shutil")

        # -------------------------- 2. 路径初始化 --------------------------
        if not report_cwd or not isinstance(report_cwd, str):
            current_script_dir = os.path.dirname(os.path.abspath(__file__))
            report_cwd = os.path.join(
                current_script_dir, "strategy", "analysis_reports")

        final_report_dir = os.path.normpath(report_cwd)
        try:
            os.makedirs(final_report_dir, exist_ok=True)
        except PermissionError:
            raise PermissionError(f"无权限创建报告目录：{final_report_dir}，请检查目录权限")

        temp_reports_dir = os.path.normpath(
            os.path.join(report_cwd, "reports"))
        try:
            os.makedirs(temp_reports_dir, exist_ok=True)
        except PermissionError:
            raise PermissionError(f"无权限创建临时报告目录：{temp_reports_dir}，请检查目录权限")

        merged_filename = f"{report_name}_analysis_reports.html"
        merged_output = os.path.normpath(
            os.path.join(temp_reports_dir, merged_filename))
        final_merged_output = os.path.normpath(
            os.path.join(final_report_dir, merged_filename))

        temp_dir = os.path.normpath(os.path.join(
            temp_reports_dir, f"temp_{report_name}"))
        try:
            os.makedirs(temp_dir, exist_ok=True)
        except PermissionError:
            raise PermissionError(f"无权限创建临时目录：{temp_dir}，请检查目录权限")

        if not os.access(temp_dir, os.W_OK):
            raise PermissionError(f"临时目录无写入权限：{temp_dir}")

        # -------------------------- 3. 生成临时报告 --------------------------
        temp_report_paths = []

        # 清空临时目录
        for file in os.listdir(temp_dir):
            file_path = os.path.join(temp_dir, file)
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
            except Exception as e:
                print(f"| 清理临时文件失败 {file_path}: {e}")

        for idx, strategy in enumerate(self.strategies):
            strategy_name = strategy.__class__.__name__

            try:
                # 生成临时报告
                strategy._qs_reports(
                    report_cwd=temp_dir,
                    report_name=f"strategy_{idx}_{strategy_name}",
                    show=False,
                    style='light',  # 强制使用亮色主题
                    **kwargs
                )
            except Exception as e:
                print(f"| 跳过策略 {idx}（{strategy_name}）：生成临时报告失败 - {str(e)}")
                continue

            # 查找生成的HTML文件
            actual_report_dir = os.path.join(temp_dir, "reports")
            if not os.path.exists(actual_report_dir):
                continue

            html_files = [f for f in os.listdir(
                actual_report_dir) if f.endswith('.html')]

            if not html_files:
                continue

            # 找到最新修改的文件
            latest_file = max(html_files, key=lambda f: os.path.getmtime(
                os.path.join(actual_report_dir, f)))
            temp_path = os.path.normpath(
                os.path.join(actual_report_dir, latest_file))

            if os.path.getsize(temp_path) == 0:
                os.remove(temp_path)
                continue

            temp_report_paths.append((idx, strategy_name, temp_path))

        if not temp_report_paths:
            raise ValueError("无有效临时报告可合并（所有策略报告生成失败）")

        # -------------------------- 4. 合并报告 --------------------------
        merged_css = set()
        merged_js = set()
        merged_content = []
        nav_items = []

        for idx, strategy_name, temp_path in temp_report_paths:
            try:
                with open(temp_path, 'r', encoding='utf-8') as f:
                    html_content = f.read()
            except Exception as e:
                print(f"| 跳过策略 {idx}（{strategy_name}）：读取临时报告失败 - {str(e)}")
                continue

            try:
                soup = BeautifulSoup(html_content, "html.parser")
            except Exception as e:
                print(f"| 跳过策略 {idx}（{strategy_name}）：解析HTML失败 - {str(e)}")
                continue

            # 提取CSS
            style_tags = soup.find_all('style')
            for style in style_tags:
                if style.string and style.string.strip():
                    merged_css.add(style.string.strip())

            # 提取JS
            script_tags = soup.find_all('script')
            for script in script_tags:
                if script.string and script.string.strip() and not script.get('src'):
                    merged_js.add(script.string.strip())

            # 提取body内容 - 修复：确保正确提取内容
            body_content = soup.find('body')
            if not body_content:
                print(f"| 跳过策略 {idx}（{strategy_name}）：临时报告无<body>内容")
                continue

            # 为每个策略创建唯一锚点
            strategy_anchor_id = f"strategy_{idx}_{strategy_name.replace(' ', '_')}"
            nav_items.append(
                f'<li><a href="#{strategy_anchor_id}" class="nav-link" data-target="{strategy_anchor_id}">策略{idx}：{strategy_name}</a></li>')

            # 包装策略内容 - 修复：确保内容正确提取和格式化
            body_html = str(body_content).replace(
                '<body>', '').replace('</body>', '')
            strategy_content = f"""
            <div id="{strategy_anchor_id}" class="strategy-report" style="display: none;">
                <h2 style="color: #2d3748; border-bottom: 2px solid #3182ce; padding-bottom: 10px; margin-top: 0;">
                    策略{idx}：{strategy_name} - QuantStats分析报告
                </h2>
                <div class="strategy-content">{body_html}</div>
            </div>
            """
            merged_content.append(strategy_content)

        # -------------------------- 5. 构建最终合并报告 --------------------------
        final_css = '\n'.join(merged_css) if merged_css else ""
        final_js = '\n'.join(merged_js) if merged_js else ""

        # 创建导航栏HTML
        nav_html = f"""
        <div id="report-nav" style="position: fixed; left: 0; top: 0; width: 250px; height: 100%; 
                overflow-y: auto; background: #f7fafc; padding: 20px; box-shadow: 2px 0 5px rgba(0,0,0,0.1); z-index: 1000;">
            <h3 style="margin: 0 0 15px 0; color: #2d3748;">策略导航</h3>
            <ul style="list-style: none; padding: 0; margin: 0;">
                {''.join(nav_items)}
            </ul>
        </div>
        """ if nav_items else ""

        final_html = f"""
        <!DOCTYPE html>
        <html lang="zh-CN">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>合并版QuantStats分析报告 - {report_name}</title>
            <style type="text/css">
                {final_css}
                /* 全局样式 */
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                    background: #fafafa;
                    color: #2d3748;
                    margin: 0;
                    padding: 0;
                }}
                .strategy-report {{
                    margin: 25px 0;
                    padding: 20px;
                    border: 1px solid #e0e0e0;
                    border-radius: 8px;
                    background: #fff;
                    display: none;
                }}
                .strategy-report.active {{
                    display: block !important;
                }}
                #report-nav {{
                    position: fixed;
                    left: 0;
                    top: 0;
                    width: 250px;
                    height: 100%;
                    overflow-y: auto;
                    background: #f7fafc;
                    padding: 20px;
                    box-shadow: 2px 0 5px rgba(0,0,0,0.1);
                    z-index: 1000;
                }}
                #report-nav ul li {{
                    margin-bottom: 10px;
                }}
                #report-nav ul li a {{
                    color: #3182ce;
                    text-decoration: none;
                    display: block;
                    padding: 8px 12px;
                    border-radius: 4px;
                    transition: background 0.2s;
                }}
                #report-nav ul li a:hover {{
                    background: #edf2f7;
                }}
                #report-nav ul li a.active {{
                    background: #3182ce;
                    color: white;
                }}
                #content-area {{
                    margin-left: 270px;
                    padding: 20px;
                }}
                /* 响应式设计 */
                @media (max-width: 768px) {{
                    #report-nav {{
                        position: relative;
                        width: 100%;
                        height: auto;
                        margin-bottom: 20px;
                    }}
                    #content-area {{
                        margin-left: 0;
                    }}
                }}
            /* 强制使用亮色主题 */
            body {{
                background-color: #ffffff !important;
                color: #000000 !important;
            }}
            .js-plotly-plot .plotly, .plot-container {{
                background-color: #ffffff !important;
            }}
            .modebar {{
                background-color: #ffffff !important;
            }}
            .main-svg {{
                background-color: #ffffff !important;
            }}
            .bg-dark {{
                background-color: #f8f9fa !important;
                color: #000000 !important;
            }}
            .text-white {{
                color: #000000 !important;
            }}
            .navbar-dark {{
                background-color: #f8f9fa !important;
            }}
            .navbar-dark .navbar-nav .nav-link {{
                color: rgba(0, 0, 0, 0.8) !important;
            }}
            .card {{
                background-color: #ffffff !important;
                border: 1px solid #e0e0e0 !important;
            }}
            .table {{
                color: #000000 !important;
            }}
            </style>
        </head>
        <body>
            {nav_html}
            <div id="content-area">
                {''.join(merged_content)}
            </div>
            <script type="text/javascript">
                {final_js}
                
                // 修复：使用更可靠的方式确保DOM完全加载
                function initReports() {{
                    // 获取所有策略报告
                    const strategyReports = document.querySelectorAll('.strategy-report');
                    
                    // 显示第一个策略报告
                    if (strategyReports.length > 0) {{
                        strategyReports[0].classList.add('active');
                        // 激活第一个导航链接
                        const firstNavLink = document.querySelector('.nav-link');
                        if (firstNavLink) {{
                            firstNavLink.classList.add('active');
                        }}
                    }}
                    
                    // 为导航链接添加点击事件
                    const navLinks = document.querySelectorAll('.nav-link');
                    navLinks.forEach(link => {{
                        link.addEventListener('click', function(e) {{
                            e.preventDefault();
                            const targetId = this.getAttribute('data-target');
                            scrollToStrategy(targetId);
                            
                            // 更新导航链接激活状态
                            navLinks.forEach(l => l.classList.remove('active'));
                            this.classList.add('active');
                        }});
                    }});
                }}
                
                // 滚动到指定策略报告
                function scrollToStrategy(strategyId) {{
                    // 隐藏所有策略报告
                    const strategyReports = document.querySelectorAll('.strategy-report');
                    strategyReports.forEach(report => {{
                        report.classList.remove('active');
                    }});
                    
                    // 显示选中的策略报告
                    const targetReport = document.getElementById(strategyId);
                    if (targetReport) {{
                        targetReport.classList.add('active');
                        
                        // 滚动到报告位置
                        window.scrollTo({{
                            top: targetReport.offsetTop - 20,
                            behavior: 'smooth'
                        }});
                    }}
                }}
                
                // 修复：多种方式确保初始化代码执行
                if (document.readyState === 'loading') {{
                    document.addEventListener('DOMContentLoaded', initReports);
                }} else {{
                    initReports();
                }}
                
                // 额外保险：延迟执行确保所有元素已加载
                setTimeout(initReports, 100);
            </script>
        </body>
        </html>
        """

        # -------------------------- 6. 保存合并报告 --------------------------
        try:
            with open(merged_output, 'w', encoding='utf-8') as f:
                f.write(final_html)
            print(
                f"| 合并报告已成功保存：{os.path.normpath(os.path.join(final_report_dir,merged_filename))}")

            # 将合并报告移动到最终目录
            shutil.copy2(merged_output, final_merged_output)

        except Exception as e:
            raise RuntimeError(f"保存合并报告失败：{str(e)}") from e

        # -------------------------- 7. 清理临时目录 --------------------------
        if not IS_JUPYTER_NOTEBOOK:
            if not keep_temp:
                try:
                    shutil.rmtree(temp_reports_dir)
                except Exception as e:
                    print(f"| 清理临时目录失败：{str(e)}，请手动删除：{temp_reports_dir}")

        # -------------------------- 8. 自动打开报告 --------------------------
        if show:
            try:
                if IS_JUPYTER_NOTEBOOK:
                    from IPython.display import display, HTML
                    import base64
                    # 读取并显示合并报告内容
                    try:
                        with open(final_merged_output, 'r', encoding='utf-8') as f:
                            html_content = f.read()

                        filename = os.path.basename(final_merged_output)
                        abs_path = os.path.abspath(final_merged_output)
                        # 将内容编码为base64
                        content_b64 = base64.b64encode(
                            html_content.encode('utf-8')).decode('utf-8')
                        data_uri = f"data:text/html;base64,{content_b64}"

                        display(HTML(f"""
                            <p>📊 合并报告:</p>
                            <!-- 浏览器渲染打开（默认显示页面效果） -->
                            <a href="{final_merged_output}" target="_blank" style="display: inline-block; margin-right: 15px; padding: 8px 12px; background: #4CAF50; color: white; text-decoration: none; border-radius: 4px;">
                                → 点击查看HTML源文件：{filename}
                            </a>
                            
                            <!-- 下载HTML源文件 -->
                            <a href="{data_uri}" download="{filename}" style="display: inline-block; padding: 8px 12px; background: #2196F3; color: white; text-decoration: none; border-radius: 4px;">
                                → 点击下载HTML源文件：{filename}
                            </a>
                        """))

                        # 正确转义HTML内容
                        escaped_html = html_content.replace(
                            "'", "&apos;").replace('"', "&quot;")

                        # 使用正确的iframe标签
                        display(HTML(f"""
                        <div style="margin: 20px 0; border: 1px solid #ddd; border-radius: 5px; padding: 10px;">
                            <h4 style="margin-top: 0;">合并报告预览</h4>
                            <iframe 
                                srcdoc='{escaped_html}' 
                                width="100%" 
                                height="{report_height}" 
                                frameborder="0"
                                style="border: 1px solid #eee; border-radius: 3px;"
                            ></iframe>
                            <p style="font-size: 12px; color: #666; margin: 10px 0 0 0;">
                                如果图表未正常显示，请点击上方链接查看完整报告
                            </p>
                        </div>
                        """))
                    except Exception as e:
                        print(f"| 显示报告内容失败: {str(e)}")
                        # 备选方案：直接显示HTML内容
                        try:
                            with open(final_merged_output, 'r', encoding='utf-8') as f:
                                html_content = f.read()
                            display(HTML(html_content))
                        except Exception as e2:
                            print(f"| 直接显示HTML也失败: {str(e2)}")

                    # 如果有临时文件且未删除，也显示临时报告
                    # if keep_temp and os.path.exists(temp_reports_dir):
                    #     print("\n📁 临时报告文件:")
                    #     display(FileLinks(temp_reports_dir,
                    #             result_html_prefix="→ "))

                else:
                    # 非 Jupyter 环境，直接在浏览器中打开
                    webbrowser.open(
                        f"file://{os.path.abspath(final_merged_output)}")
                    # print(f"| 已自动打开合并报告（浏览器）")

            except Exception as e:
                print(f"| 显示报告失败：{str(e)}，请手动打开文件：{final_merged_output}")
        return self

    def get_results(self, select: int | str | list[int] = 'all') -> list[list[pd.DataFrame]]:
        """## 获取回测结果数据（DataFrame格式，含行情、交易、账户净值等）
        -------------"""
        # 校验：回测未完成时，打印警告并返回
        if not self.__is_finish:
            self.strategies[select].logger.warning('策略尚未回测（请先调用run()执行回测）')
            return
        print(f"策略个数：{len(self.strategies)}")
        # 处理策略选择：统一为列表格式
        select_list = select if isinstance(select, list) else [select,]
        # 返回选中策略的回测结果（每个策略对应一个DataFrame列表）
        return [t.get_results() for i, t in enumerate(self.strategies)
                if 'all' in select_list or i in select_list]

    def qs_stats(self, select: int = 0) -> Stats:
        """## 获取QuantStats性能统计对象（支持调用多种性能/风险指标方法）

        ### Method:支持的指标方法包括（部分）
            - 收益类：profit_ratio（盈利比）、cagr（年化收益率）、avg_return（平均收益率）
            - 风险类：max_drawdown（最大回撤）、volatility（波动率）、var（风险价值）
            - 风险收益比：sharpe（夏普率）、sortino（索提诺率）、calmar（卡玛率）
            - 其他：win_rate（胜率）、consecutive_wins（最大连续盈利次数）、drawdown_details（回撤详情）

        Args:
            select (int, optional): 策略索引（多策略时选择，默认0）. Defaults to 0.

        Returns:
            Stats: QuantStats统计对象（可调用上述指标方法）
        """
        # 校验：回测未完成时，打印警告并返回
        if not self.__is_finish:
            return self.strategies[select].logger.warning('策略尚未回测（请先调用run()执行回测）')

        # 校验策略索引合法性（超出范围时默认取0）
        select = select if isinstance(
            select, int) and 0 <= select < self.__multi_num else 0
        # 返回选中策略的Stats对象
        return self.strategies[select]._stats

    def qs_plot(self, select: int = 0) -> QSPlots:
        """## 获取QuantStats可视化对象（支持绘制多种回测结果图表）

        ### Method:支持的绘图方法包括（部分）
            - returns（收益率曲线）、drawdown（回撤曲线）、histogram（收益率分布直方图）
            - monthly_heatmap（月度收益热力图）、rolling_sharpe（滚动夏普率）
            - yearly_returns（年度收益率）、distribution（收益分布）

        Args:
            select (int, optional): 策略索引（多策略时选择，默认0）. Defaults to 0.

        Returns:
            QSPlots: QuantStats可视化对象（可调用上述绘图方法）
        """
        # 校验：回测未完成时，打印警告并返回
        if not self.__is_finish:
            return self.strategies[select].logger.warning('策略尚未回测（请先调用run()执行回测）')

        # 校验策略索引合法性（超出范围时默认取0）
        select = select if isinstance(
            select, int) and 0 <= select < self.__multi_num else 0
        # 返回选中策略的QSPlots对象
        return self.strategies[select]._qs_plots

    def qs_metrics(self, benchmark=None, rf=0., display=True,
                   mode='basic', sep=False, compounded=True,
                   periods_per_year=252, prepare_returns=True,
                   match_dates=False, **kwargs):
        """## 生成QuantStats指标报告（仅数值指标，无图表）
        包括收益率、风险、风险收益比等核心指标"""
        # 校验：回测未完成时，打印警告并返回
        if not self.__is_finish:
            return self.strategies[0].logger.warning('策略尚未回测（请先调用run()执行回测）')

        from quantstats.reports import metrics  # 导入QuantStats指标报告函数
        # 为所有策略生成指标报告
        [metrics(t._net_worth, benchmark, rf, display, mode, sep, compounded, periods_per_year,
                 prepare_returns, match_dates, **kwargs) for t in self.strategies]
        return self

    def qs_plots(self, benchmark=None, grayscale=False, figsize=(8, 5), mode='basic', compounded=True,
                 periods_per_year=252, prepare_returns=True, match_dates=False):
        """## 生成QuantStats可视化报告（仅图表，无数值指标）
        包括收益率曲线、回撤曲线、收益分布等图表"""
        # 校验：回测未完成时，打印警告并返回
        if not self.__is_finish:
            return self.strategies[0].logger.warning('策略尚未回测（请先调用run()执行回测）')

        from quantstats.reports import plots  # 导入QuantStats绘图报告函数
        # 为所有策略生成可视化报告
        [plots(t._net_worth, benchmark, grayscale, figsize, mode, compounded,
               periods_per_year, prepare_returns, match_dates) for t in self.strategies]
        return self

    def qs_basic(self, benchmark=None, rf=0., grayscale=False,
                 figsize=(8, 5), display=True, compounded=True,
                 periods_per_year=252, match_dates=False):
        """## 生成QuantStats基础报告（简化版，含核心指标和关键图表）"""
        # 校验：回测未完成时，打印警告并返回
        if not self.__is_finish:
            return self.strategies[0].logger.warning('策略尚未回测（请先调用run()执行回测）')

        from quantstats.reports import basic  # 导入QuantStats基础报告函数
        # 为所有策略生成基础报告
        [basic(t._net_worth, benchmark, rf, grayscale, figsize, display, compounded,
               periods_per_year, match_dates) for t in self.strategies]
        return self

    def qs_full(self, benchmark=None, rf=0., grayscale=False,
                figsize=(8, 5), display=True, compounded=True,
                periods_per_year=252, match_dates=False):
        """## 生成QuantStats完整报告（详细版，含所有指标、图表、分析结论）"""
        # 校验：回测未完成时，打印警告并返回
        if not self.__is_finish:
            return self.strategies[0].logger.warning('策略尚未回测（请先调用run()执行回测）')

        from quantstats.reports import full  # 导入QuantStats完整报告函数
        # 为所有策略生成完整报告
        [full(t._net_worth, benchmark, rf, grayscale, figsize, display, compounded,
              periods_per_year, match_dates) for t in self.strategies]
        return self

    def qs_html(self, benchmark=None, rf=0., grayscale=False,
                title='Strategy Tearsheet', output=None, compounded=True,
                periods_per_year=252, download_filename='quantstats-tearsheet.html',
                figfmt='svg', template_path=None, match_dates=False, **kwargs):
        """## 生成HTML格式QuantStats报告（可在浏览器打开/保存，支持分享）"""
        # 校验：回测未完成时，打印警告并返回
        if not self.__is_finish:
            return self.strategies[0].logger.warning('策略尚未回测（请先调用run()执行回测）')

        from quantstats.reports import html  # 导入QuantStats HTML报告函数
        # 为所有策略生成HTML报告
        [html(t._net_worth, benchmark, rf, grayscale, title, output, compounded, periods_per_year,
              download_filename, figfmt, template_path, match_dates, **kwargs) for t in self.strategies]
        return self

    def qs_iDisplay(self, *objs, include=None, exclude=None, metadata=None, transient=None, display_id=None, **kwargs):
        """## Jupyter Notebook中交互式显示QuantStats报告（支持动态更新）"""
        # 校验：回测未完成时，打印警告并返回
        if not self.__is_finish:
            return self.strategies[0].logger.warning('策略尚未回测（请先调用run()执行回测）')

        from quantstats.reports import iDisplay  # 导入交互式显示函数
        # 为所有策略生成交互式报告
        [iDisplay(*objs, include, exclude, metadata, transient,
                  display_id, **kwargs) for t in self.strategies]
        return self

    def qs_iHTML(self, data=None, url=None, filename=None, metadata=None):
        """## Jupyter Notebook中显示HTML格式QuantStats报告（支持本地/远程HTML）"""
        # 校验：回测未完成时，打印警告并返回
        if not self.__is_finish:
            return self.strategies[0].logger.warning('策略尚未回测（请先调用run()执行回测）')

        from quantstats.reports import iHTML  # 导入HTML交互式显示函数
        # 为所有策略显示HTML报告
        [iHTML(data, url, filename, metadata) for t in self.strategies]
        return self
