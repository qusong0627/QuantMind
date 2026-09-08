from __future__ import annotations
from ..indicators import (KLine, pd, KLineType,
                          FILED, Iterable,
                          TYPE_CHECKING, BtID)
from ..utils import (cache, np, Callable, _time,
                     BASE_DIR, pd, BtIndicatorDataSet,
                     Any, time_to_datetime, timedelta,
                     KLinesSet, time, datetime,sys,
                     os,  partial, Literal, FilteredOutputRedirector,
                     Config, get_cycle, read_unknown_file,
                     format_3col_report, qs_stats, FILED, loadData,
                     save_and_generate_utils,)
from .stats import Stats
from .qs_plots import QSPlots
import inspect
import ast
from ..order import OrderType,OrderSide
from ..other import SizeType, CommissionType, time_to_ns_timestamp,time_to_s_timestamp

if TYPE_CHECKING:
    from ..indicators import IndSeries, IndFrame, TqAccount, Line
    from .strategy import Strategy
    from ..utils import TqApi, BtAccount, BtPosition, Position, TqObjs, Params, OpConfig
    from ..elegantrl.train.config import Config as RlConfig
    from pytdx.hq import TdxHq_API
    import baostock as bs
    import akshare as ak
    import torch
    from ..logger import Logger
    from ..order import Order
    from ..stop import BtStop
    from tqsdk.objs import Order as TqOrder


class StrategyBase:
    """
    ## 量化策略基础抽象类（Strategy的父类）
    ### 核心定位：
    - 封装量化策略的通用能力，包括数据获取、回测/实盘调度、交易执行、指标管理、强化学习（RL）集成、结果分析等，
    - 为子类策略提供标准化接口与底层实现，实现回测与实盘的无缝切换

    ### 核心职责：
    1. 数据管理：支持从TQSDK（期货）、PyTDX（股票）、数据库、外部DataFrame获取K线数据，自动补充必要字段
    2. 交易执行：统一封装买入、卖出、目标仓位设置等交易接口，区分回测（BtAccount）与实盘（TqAccount）逻辑
    3. 回测调度：实现回测循环迭代，处理止损逻辑、账户历史更新，支持RL模式与普通策略模式
    4. 指标与绘图：管理指标数据集合，整理绘图所需数据结构，支持自定义指标的可视化配置
    5. 结果分析：集成QuantStats工具，计算回测指标（夏普比率、最大回撤等）、生成HTML报告、打印关键统计信息
    6. 强化学习：提供RL特征处理、数据增强、智能体加载/训练接口，适配ElegantRL框架

    ### 关键设计：
    - 抽象方法（reset/step/start/stop）：需子类重写实现具体策略逻辑
    - 数据集合（_btklinedataset/_btindicatordataset）：统一管理K线与指标数据，确保数据一致性
    - 配置驱动（config）：通过Config对象控制回测/实盘参数（如手续费、滑点、绘图开关）
    - 模式兼容：通过_is_live_trading/_isoptimize标记区分实盘、回测、参数优化模式
    """
    # 策略参数字典（子类通过重写定义策略参数，如ma_length=5）
    params: Params = {}
    # 策略配置对象（控制回测/实盘参数，如手续费、滑点、绘图开关）
    config: Config = Config()
    # 参数优化配置对象（用于参数搜索）
    op_config: OpConfig | None = None
    # 策略ID（用于多策略区分）
    _sid: int = 0
    # 天勤TQApi实例（实盘模式使用）
    _api: TqApi
    # 当前收盘价数组（实时更新）
    _current_close: np.ndarray
    # 当前时间数组（实时更新）
    _current_datetime: np.ndarray
    # 自定义指标名称映射（用于绘图）
    _custom_ind_name: dict
    # K线数据集合（管理所有KLine实例，支持dict或KLinesSet类型）
    _btklinedataset: KLinesSet[str, KLine] | dict[str, KLine]
    # 指标数据集合（管理所有Line/IndSeries/IndFrame实例）
    _btindicatordataset: BtIndicatorDataSet[str, KLine] | dict[str, Line | IndSeries | IndFrame]
    _tqobjs: dict[str, TqObjs]
    # 账户对象（回测用BtAccount，实盘用TqAccount）
    _account: BtAccount | TqAccount
    # 是否启用止损（True表示至少一个合约配置了止损）
    _isstop: bool = False
    # 是否处于参数优化模式
    _isoptimize: bool = False
    # 是否处于实盘交易模式
    _is_live_trading: bool = False
    # 是否首次启动策略
    _first_start: bool = False
    # 更新长度
    update_length: int = 10
    th: torch
    # 是否启用强化学习（RL）模式
    rl: bool = False
    # 初始化标记（内部使用）
    _init_: bool = False
    # PyTDX API实例（股票数据获取用）
    _tdxapi = None
    # 参数优化的目标值（用于记录最优结果）
    _target_train: int = 0.
    # 是否启用快速启动模式（简化初始化流程）
    quick_start: bool = False
    # 是否启用快速实盘模式（简化实盘初始化）
    quick_live: bool = False
    # 回测结果列表（每个元素对应一个Broker的历史数据DataFrame）
    _results: list[pd.DataFrame]  # 回测结果=[]
    # 回测当前索引（迭代K线数据用）
    _btindex: int = -1  # 索引
    # 策略初始化状态（True表示初始化完成）
    _in_next_context: bool = False  # 策略初始化状态
    # 图表保存名称（默认'plot'）
    _plot_name: str = 'plot'  # 图表保存名称
    # 回测报告保存名称（默认"qs_reports"）
    _qs_reports_name: str = "qs_reports"  # 回测报告名称
    _id_dir: str = ""
    # 原始数据列表（内部使用）
    _datas: list[pd.DataFrame]
    # 绘图数据结构（供前端/绘图工具使用，包含K线、指标、配置等）
    _plot_datas: list
    # 强制绘制收益曲线
    _profit_plot: bool = False
    # 记录是否使用策略回话
    _isreplay: bool = False
    # 初始持仓记录（实盘模式用）
    _init_trades: list
    # 指标绘图配置记录（包含是否显示、名称、线型等）
    _indicator_record: list
    # RL模式的信号特征数组（处理后的特征数据）
    _signal_features: np.ndarray | None = None
    # 账户净值序列（用于回测分析）
    _net_worth: pd.Series | None = None
    # 回测统计分析对象（计算夏普比率、最大回撤等）
    _stats: Stats | None = None
    # 回测绘图对象（生成收益曲线、回撤曲线等）
    _qs_plots: QSPlots | None = None
    # RL训练配置对象（ElegantRL的Config）
    _rl_config: RlConfig | None = None
    # RL评估环境（用于验证训练后的智能体）
    evaluator_env: Strategy
    # RL环境参数（状态维度、动作维度等）
    _env_args: dict
    # RL当前状态数组
    _state: np.ndarray
    # RL智能体（actor网络）
    _actor = None
    # RL数据增强函数列表
    _data_enhancement_funcs: list[Callable] = []
    # 是否已加载数据增强函数
    _if_data_enhancement: bool = False
    # RL观测窗口大小（默认10）
    window_size: int = 10
    _strategy_replay: bool
    # _executed: bool
    _akshare: ak
    _baostock: bs
    _pytdx: TdxHq_API
    # 日志输出
    _logger: Logger
    # 切换策略
    _data_patching: bool = False
    _isbacktrader_form_signals: bool = False
    _bt_from_signals_func: Callable | None = None
    _light_chart: bool = False
    _default_order_type: OrderType = OrderType.Close
    _call_func: Callable
    __auto_process_features: bool = False
    
    @classmethod
    def copy(cls, **kwargs) -> Strategy:
        """
        ## 复制策略类（创建新的策略子类）
        - 用于动态生成多个策略子类，支持只传差异参数

        ### Kwargs:
            name (str): 新策略类名（不传则自动从赋值变量名推断）
            额外的类属性：覆盖原类同名属性
            对 dict 类型属性（如 params、config）执行深度合并：
            以父类值为基础，kwargs 中的同名键覆盖

        Returns:
            Strategy 子类（需实例化后使用）

        Examples:
            >>> # 只改 symbol，其余参数继承父类
            >>> V2609 = PolyFitTrendStrategy.copy(params=dict(symbol="DCE.v2609"))
        """
        name = kwargs.pop("name", cls.__get_assigned_variable_name("copy"))
        assert name and isinstance(name, str), "请使用kwargs:name=...设置策略名称"
        # 对 dict 类型属性执行深度合并（父类值为基础，kwargs 覆盖同名键）
        for key in list(kwargs.keys()):
            if isinstance(kwargs[key], dict) and hasattr(cls, key) and isinstance(getattr(cls, key), dict):
                kwargs[key] = {**getattr(cls, key), **kwargs[key]}
        # 合并原类属性与kwargs，kwargs优先级更高
        kwargs = {**cls.__dict__, **kwargs}
        # 动态创建新子类
        return type(name, (cls,), kwargs)

    @classmethod
    def __get_assigned_variable_name(cls, method_name) -> str | None:
        """## 获取当前方法调用被赋值给的变量名"""
        frame = inspect.currentframe().f_back.f_back  # 跳过当前方法和调用它的包装器
        line = inspect.getframeinfo(frame).code_context[0].strip()

        try:
            tree = ast.parse(line)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and node.targets:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            if isinstance(node.value, ast.Call):
                                call_node = node.value
                                called_func_name = None
                                if isinstance(call_node.func, ast.Name):
                                    called_func_name = call_node.func.id
                                elif isinstance(call_node.func, ast.Attribute):
                                    called_func_name = call_node.func.attr

                                if called_func_name == method_name:
                                    return target.id
        except:
            pass

    def _calculate_optimization_targets(self, ismax, target):
        """
        ## 获取参数优化的目标结果（用于参数优化时评估单组参数性能）
        - 根据目标字段（如收益率、夏普比率）计算结果，并更新最优目标值

        Args:
            ismax (bool): 目标是否为最大化（如收益率最大化设为True，风险最小化设为False）
            target (Iterable): 目标字段列表（如["total_profit", "sharpe_ratio"]）

        Returns:
            tuple: 目标字段对应的结果元组
        """
        results = []
        for _target in target:
            # 从Stats对象获取目标字段值（支持方法调用，如sharpe()）
            result = getattr(self._stats, _target)()
            # 若结果为序列（如时间序列），取最后一个值；否则直接使用
            result = result if isinstance(result, float) else list(result)[-1]
            # 处理None值（默认为0）
            result = result if result else 0.
            results.append(result)
        # 更新最优目标值（根据ismax判断最大化/最小化）
        if ismax:
            if results[0] > self._target_train:
                self._target_train = results[0]
                print(f"best train results:{results}")
        else:
            if results[0] < self._target_train:
                self._target_train = results[0]
                print(f"best train results:{results}")
        return tuple(results)

    @staticmethod
    @cache
    def _pytdx_category_dict(category="pytdx_category_dict") -> dict:
        """
        ## 缓存PyTDX的周期-类别编码映射（静态方法，缓存结果避免重复计算）
        - PyTDX通过类别编码区分不同K线周期，该方法提供周期（秒/字符串）到编码的映射

        Args:
            category (str): 映射名称（预留参数，无实际作用）

        Returns:
            dict: 周期到PyTDX类别的映射，键为周期（秒或'D'/'W'等），值为类别编码
        """
        return dict(zip(
            [60, 5*60, 15*60, 30*60, 60*60, 60*60*24, 'W', 'M', 'S', 'Y'],
            [7, 0, 1, 2, 3, 4, 5, 6, 10, 11]
        ))

    def _get_pytdx_data(self, symbol, cycle, lenght=800, **kwargs):
        """
        ## 通过PyTDX获取股票K线数据（内部方法，供get_data调用）
        - 支持分批次获取（PyTDX单次最大获取800根），自动合并数据并格式化

        Args:
            symbol (str): 股票代码（如'600000'，沪市前加'6'，深市前加'0'）
            cycle (int): K线周期（秒，如60=1分钟，300=5分钟）
            lenght (int): 数据长度（默认800，最大支持2400）

        Returns:
            pd.DataFrame: 格式化后的K线数据，包含['datetime', 'open', 'high', 'low', 'close', 'volume', 'symbol', 'duration', 'price_tick', 'volume_multiple']

        Raises:
            AssertionError: 获取数据为空时触发
        """
        if not hasattr(self, "_pytdx"):
            try:
                from pytdx.hq import TdxHq_API
                from pytdx.config.hosts import hq_hosts
                self._pytdx = TdxHq_API()
            except ImportError:
                raise ImportError("请安装pytdx（pip install pytdx）以使用PyTDX数据源")
        assert self._pytdx, "PyTDX API初始化失败"

        # PyTDX服务器配置（默认取第5个服务器）
        ip = kwargs.pop('ip', hq_hosts[4][1])
        port = kwargs.pop('port', hq_hosts[4][2])
        with self._tdxapi.connect(ip, port):
            # 根据周期获取PyTDX类别编码
            category = self._pytdx_category_dict().get(cycle)
            # 判断市场（沪市代码以'6'开头，深市以'0'开头）
            mk = 1 if symbol[0] == '6' else 0
            data = []
            # 计算分批次数量（PyTDX单次最大800根）
            div, mod = divmod(lenght, 800)
            # 处理余数批次（若mod>0，先获取余数部分）
            if mod:
                data += self._tdxapi.get_security_bars(
                    category, mk, symbol, div*800, mod)
            # 处理完整批次（每次800根）
            for i in range(div):
                data += self._tdxapi.get_security_bars(
                    category, mk, symbol, i*800, 800)
            # 转换为DataFrame并格式化
            data = self._tdxapi.to_df(data)
            assert not data.empty, "获取数据失败"
            # 字段映射与格式处理
            data['volume'] = data.vol  # PyTDX的成交量字段为'vol'，统一为'volume'
            data.datetime = pd.to_datetime(data.datetime)  # 转换时间格式
            data = data[FILED.ALL]
        # tdxapi.close()
        return data

    def _get_baostock_data(self, symbol, duration_seconds, data_length=800, **kwargs):
        if not hasattr(self, "_baostock"):
            try:
                with FilteredOutputRedirector():
                    import baostock as bs
                    self._baostock = bs
                    bs.logout()
            except ImportError:
                raise ImportError(
                    "请安装baostock（pip install baostock）以使用Baostock数据源")

        assert self._baostock, "baostock初始化失败"
        # 1. Baostock参数映射
        cycle_map = {
            300: '5',        # 5分钟
            900: '15',       # 15分钟
            1800: '30',      # 30分钟
            3600: '60',      # 60分钟
            86400: 'd',      # 日线
            604800: 'w',     # 周线
            2592000: 'm'     # 月线
        }
        if isinstance(duration_seconds, (float, int)):
            duration_seconds = int(duration_seconds)
            assert duration_seconds in cycle_map, f"Baostock不支持{duration_seconds}秒周期，支持{list(cycle_map.keys())}"
            bs_frequency = cycle_map[duration_seconds]

        # 股票代码映射
        bs_symbol = f"sh.{symbol}" if symbol.startswith(
            '6') else f"sz.{symbol}"

        # 2. 处理日期参数
        end_date = kwargs.get('end_date', None)
        start_date = kwargs.get('start_date', None)

        # 如果没有提供end_date，使用当前日期
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')
        data_length = data_length if isinstance(
            data_length, int) and data_length > 0 else 800
        # 如果没有提供start_date，根据data_length计算
        if start_date is None:
            # 估算每个周期对应的天数（保守估计）
            if duration_seconds >= 86400:  # 日线及以上
                days_per_period = duration_seconds / 86400
            else:  # 分钟线，假设每天有4小时交易时间
                days_per_period = duration_seconds / (4 * 3600) / 24

            # 计算需要的天数并转换为日期
            required_days = data_length * days_per_period * 1.5  # 增加50%缓冲考虑非交易日
            start_date = (datetime.now() -
                          timedelta(days=required_days)).strftime('%Y-%m-%d')

        # 4. 分段获取数据
        data_list = []
        batch_size = 200  # 每批次获取200天数据，可根据需要调整

        # 将日期字符串转换为datetime对象以便计算
        start_dt = datetime.strptime(start_date, '%Y-%m-%d')
        end_dt = datetime.strptime(end_date, '%Y-%m-%d')
        fields = kwargs.pop("fields", "date,open,high,low,close,volume")
        adjustflag = kwargs.pop("adjustflag", "1")
        # 计算需要分成多少批次
        total_days = (end_dt - start_dt).days
        num_batches = max(1, (total_days + batch_size - 1) // batch_size)

        for i in range(num_batches):
            # 计算当前批次的起始和结束日期
            batch_start_dt = start_dt + timedelta(days=i * batch_size)
            batch_end_dt = min(
                start_dt + timedelta(days=(i + 1) * batch_size), end_dt)

            batch_start_date = batch_start_dt.strftime('%Y-%m-%d')
            batch_end_date = batch_end_dt.strftime('%Y-%m-%d')

            # print(f"正在获取第{i+1}/{num_batches}批次数据: {batch_start_date} 至 {batch_end_date}")

            # 调用Baostock接口获取K线
            rs = self._baostock.query_history_k_data_plus(
                code=bs_symbol,
                fields=fields,
                frequency=bs_frequency,
                adjustflag=adjustflag,
                start_date=batch_start_date,
                end_date=batch_end_date,
            )

            if rs.error_code != '0':
                # print(f"Baostock第{i+1}批次获取失败：{rs.error_msg}，跳过该批次")
                continue

            # 收集当前批次数据
            while rs.next():
                data_list.append(rs.get_row_data())

            # 添加短暂延迟避免请求过于频繁
            # time.sleep(0.1)

        # 5. 数据格式化
        if not data_list:
            raise ValueError("Baostock获取数据为空")

        data = pd.DataFrame(data_list, columns=[
                            'date', 'open', 'high', 'low', 'close', 'volume'])

        # 数据类型转换
        numeric_cols = ['open', 'high', 'low', 'close', 'volume']
        data[numeric_cols] = data[numeric_cols].astype(float)
        data['datetime'] = pd.to_datetime(data['date'])

        # 按时间排序并去重
        data = data.sort_values(
            'datetime').drop_duplicates(subset=['datetime'])

        # 如果指定了data_length，截取指定长度的数据
        if isinstance(data_length, int) and len(data) > data_length:
            data = data.tail(data_length)

        # 保留必要列
        data = data[['datetime', 'open', 'high', 'low', 'close', 'volume']]

        # 6. 登出Baostock
        with FilteredOutputRedirector():
            bs.logout()
        return data.reset_index(drop=True)

    def _get_akshare_data(self, symbol: str, duration_seconds, data_length: int | None = None, **kwargs):
        if not hasattr(self, "_akshare"):
            try:
                import akshare as ak
                self._akshare = ak
            except ImportError:
                raise ImportError(
                    "请安装akshare（pip install akshare）以使用AkShare数据源")
        assert self._akshare, "akshare初始化失败"

        # 1. AkShare参数映射（周期/数据长度）
        # 1.1 周期映射：duration_seconds -> akshare的period参数
        cycle_map = {
            60: '1',
            300: '5',        # 5分钟
            900: '15',       # 15分钟
            1800: '30',      # 30分钟
            3600: '60',      # 60分钟
            86400: 'daily',      # 日线
            604800: 'weekly',     # 周线
            2592000: 'monthly'     # 月线
        }
        period = kwargs.pop("period", None)
        if period not in ["1", "5", "15", "30", "60", "daily", "weekly", "monthly"]:
            if duration_seconds in cycle_map:
                period = cycle_map[duration_seconds]
            else:
                period = '1'
        start_date = kwargs.pop("start_date", "19700101")
        end_date = kwargs.pop("end_date", "20500101")
        adjust = kwargs.pop("adjust", "qfq")
        timeout = kwargs.pop("timeout", 30)  # 默认 30 秒超时
        isreset = True

        # 带重试的 AkShare 数据获取
        max_retries = 3
        last_error = None
        for attempt in range(max_retries):
            try:
                if symbol.isdigit():  # 股票
                    if period not in ["daily", "weekly", "monthly"]:
                        period = "daily"
                    data: pd.DataFrame = self._akshare.stock_zh_a_hist(
                        symbol=symbol,
                        period=period,
                        start_date=start_date,
                        end_date=end_date,
                        adjust=adjust,
                        timeout=timeout
                    )
                else:  # 期货
                    if period in ["1", "5", "15", "30", "60"]:
                        data = self._akshare.futures_zh_minute_sina(symbol, period)
                        isreset = False
                    else:
                        start_date = kwargs.pop("start_date", "19900101")
                        end_date = kwargs.pop("end_date", "20500101")
                        data = self._akshare.futures_hist_em(
                            symbol, period, start_date, end_date)
                break  # 成功则跳出重试循环
            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    wait = 2 ** attempt  # 1s, 2s, 4s 指数退避
                    time.sleep(wait)
                    continue
                # 最后一次重试仍失败，抛出更友好的错误
                raise ConnectionError(
                    f"AkShare 获取股票 {symbol} 数据失败（重试{max_retries}次）: {last_error}\n"
                    f"请检查: 1) 网络连接是否正常 2) 股票代码是否正确 "
                    f"3) akshare 版本是否最新（pip install --upgrade akshare）"
                ) from last_error

        if isreset:
            data.rename(columns={
                "日期": "datetime",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
            }, inplace=True)
        # 3. 数据格式化（AkShare返回的index为date，需转换为datetime列）
        # data.reset_index(drop=True, inplace=True)
        data['datetime'] = pd.to_datetime(data['datetime'])
        # data.rename(columns={'date': 'datetime'}, inplace=True)  # 统一列名为datetime

        # 3.1 保留必要列（AkShare可能返回extra列，如amount，需过滤）
        data = data[FILED.ALL]

        # 3.2 截取指定长度（从最新数据往前取）
        # 1.2 数据长度默认10000，AkShare返回数据已按时间升序排列
        if isinstance(data_length, int) and data_length > 0 and data_length < len(data):
            data = data.tail(data_length).reset_index(drop=True)
        # 4. 数据校验
        assert not data.empty, "AkShare获取数据为空"
        return data

    def get_kline(self, symbol: str | pd.DataFrame = None, duration_seconds: int = 60, data_length: int | None = None, **kwargs) -> KLine:
        """## 统一获取K线数据的接口，支持多数据源适配，自动兼容股票/期货品种及回测/实盘场景

        ### 📘 **文档参考**:
        - https://www.minibt.cn/minibt_basic/1.6minibt_strategy_data_retrieval/

        ### 功能说明：
        - 提供标准化数据获取入口，可从本地CSV、TQSDK（期货）、PyTDX/baostock/akshare（股票）、
          外部DataFrame等多种数据源获取K线，自动归一化为框架内部的KLine对象
        - 支持数据本地保存（save参数）、合约信息自动补充（最小变动单位price_tick、合约乘数volume_multiple等）
        - 自动生成KLine唯一标识ID，关联策略与数据索引，确保多策略/多数据源场景下数据不混淆

        ### 数据源优先级（回测模式）：
        1. 本地CSV文件（BASE_DIR/data/test/{symbol}.csv）
        2. symbol路径直接引用的文件（如绝对路径）
        3. TQSDK在线获取期货数据（需user_name+password，支持自动初始化_api）
        4. 外部传入的pd.DataFrame（需包含OHLCV字段，缺失字段自动补充）
        5. 股票在线数据源（akshare/pytdx/baostock，通过data_source参数切换）
        6. 已缓存的_api中的期货数据（无需重复输入账号密码）
        7. _datas中已加载的原始数据（按名称匹配）

        Args:
            symbol (str | pd.DataFrame, optional): 数据标识，支持三种形式
                - ``str``：合约代码（期货如 ``\"SHFE.rb2410\"``）或股票代码（如 ``\"600000\"``），
                  也可直接传入本地文件路径（如 ``\"D:/data/rb.csv\"``）
                - ``pd.DataFrame``：外部K线数据，需包含必要字段
                  ``['datetime', 'open', 'high', 'low', 'close', 'volume']``，
                  缺失的合约信息字段（symbol/duration/price_tick/volume_multiple）将自动补充默认值
                - ``None``：不传入symbol时，将尝试从 ``_datas`` 或 ``_api`` 中匹配数据
                  （不推荐，一般在调用方明确知晓数据已预加载时使用）
            duration_seconds (int, optional): K线周期（单位：秒），默认60秒（1分钟线）
                - 常见周期：60(1分钟)、300(5分钟)、900(15分钟)、1800(30分钟)、3600(1小时)、
                  86400(日线)、604800(周线)、2592000(月线)
                - 不同数据源支持的周期范围不同（如akshare股票仅支持daily/weekly/monthly），
                  不支持的周期会自动回退到默认值
            data_length (int | None, optional): 需获取的数据长度（K线根数）
                - 若为 ``None``：实盘默认300根 / 回测默认10000根
                - 实盘模式下：若传入值 < 10，自动修正为300（确保有足够计算窗口）
                - 回测模式下：若传入值 < 300，自动修正为10000（保证策略各类指标所需的起算长度）
                - ⚠️ 回测模式最终实际使用的数据长度 = data_length + min_start_length（策略预热）

            **kwargs: 额外配置参数（按类别分述）
                **数据保存相关**：
                    - save (str | bool)：是否将获取到的数据保存为本地CSV
                    - 若为str，使用该值作为文件名（不含.csv扩展名）
                    - 若为True，使用 ``name`` 作为文件名

                **TQSDK期货数据相关**（需安装tqsdk）：
                    - user_name (str)：天勤账号
                    - password (str)：天勤密码
                    - chart_id (int)：天勤K线图表ID（传入_get_kline_serial，默认为None）
                    - adj_type (str)：复权类型（None=不复权）

                **股票数据源相关**：
                    - data_source (str)：股票数据源类型，支持 ``'akshare'``(默认)、``'pytdx'``、
                      ``'baostock'``，不区分大小写
                    - ip (str)：PyTDX服务器IP（_get_pytdx_data中使用）
                    - port (int)：PyTDX服务器端口（_get_pytdx_data中使用）
                    - start_date / end_date (str)：日期范围过滤（_get_baostock_data / _get_akshare_data中使用）
                    - period (str)：周期标识（_get_akshare_data中使用，如'1'/'5'/'15'/'daily'）
                    - adjust (str)：复权方式，默认'qfq'前复权（_get_akshare_data中使用）
                    - fields (str)：查询字段（_get_baostock_data中使用，默认"date,open,high,low,close,volume"）
                    - timeout (int)：接口超时时间（_get_akshare_data中使用）

        Returns:
            KLine: 封装后的K线数据对象，包含：
                - 原始K线数据（pd.DataFrame）
                - 合约基础信息（price_tick最小变动单位、volume_multiple合约乘数、symbol合约代码）
                - 指标计算接口（.sma() / .ewm() / .rolling() 等链式调用）
                - 数据标识ID（关联策略ID与数据索引，用于多数据源追踪）
                - 实盘模式下额外包含 tq_object / tq_tick 原始对象（供运行时实时更新）

        ### 处理流程（按序执行）：
        1. 保存相关kwarg提前弹出（``save``），避免污染后续传递链
        2. 确保 ``_btklinedataset`` 已初始化（K线数据集容器，管理所有KLine实例）
        3. 生成KLine唯一标识 ``btid``：基于策略ID（``_sid``）和数据集索引（``_btklinedataset.num``）
        4. **缓存复用检查**（仅优化/实盘模式生效）：
            - 若 ``_first_start=True`` 且当前为优化/实盘模式：直接返回 ``None``
            - ``Strategy.__setattr__()`` 会拦截 ``None`` 赋值，保持 ``self.data`` 指向首轮缓存的
              KLine，从而跳过重复的数据获取（见 ``strategy.py`` 中 ``__setattr__`` 的缓存分支）
        5. **模式分支**：
            - **实盘模式**（``_is_live_trading=True``）：通过TQSDK实时获取K线序列（默认300根），
              同时获取实时tick报价，格式化时间字段
            - **回测/优化模式**（``_is_live_trading=False``）：多数据源自动适配
                - a. symbol为字符串 → 优先本地CSV → 次优TQSDK在线期货数据
                - b. symbol为DataFrame → 校验并补充字段
                - c. symbol为纯数字字符串 → 股票数据源（akshare/pytdx/baostock）
                - d. _api已初始化 → 直接通过TQSDK获取（复用已有连接）
                - e. _datas中有已加载数据 → 按名称匹配后复制
        6. 若指定 ``save`` 参数：将数据保存为CSV至本地（``BASE_DIR/data/test/``），
           并自动更新 ``data/utils.py``（生成 ``LocalDatas`` 引用工具类）
        7. 数据截取：取最新 ``data_length`` 根K线（确保不小于最小要求）
        8. 数据校验三重断言：
            - 类型必须是 ``pd.DataFrame``
            - 数据不能为空
            - 必须包含 ``FILED.ALL`` 定义的所有必要字段（datetime/OHLCV/symbol/duration/price_tick/volume_multiple）
        9. 封装为 ``KLine`` 对象并返回，传入tq_object/tq_tick等内部附件供后续运行时使用

        Raises:
            AssertionError: 在以下条件不满足时触发
                - 数据类型非 ``pd.DataFrame``
                - 数据为空
                - 数据缺少 ``FILED.ALL`` 定义的必要字段
                - 股票数据源不在 ``['pytdx', 'baostock', 'akshare']`` 范围内
            ValueError: 所有数据获取途径均失败时触发（如无本地文件、无网络、无_api、无_datas）
            ImportError: 未安装指定的股票数据源库却尝试获取股票数据时触发（如未安装pytdx却指定data_source='pytdx'）

        Examples:
            >>> # [回测] 从本地数据文件获取K线
            >>> self.data = self.get_kline(LocalDatas.v2601_300, height=500)

            >>> # [回测] 从股票代码获取（自动通过akshare下载）
            >>> self.data = self.get_kline('600000', duration_seconds=86400, data_length=500)

            >>> # [回测] 从期货代码获取（自动通过TQSDK下载，需账号密码）
            >>> self.data = self.get_kline('SHFE.rb2410', duration_seconds=60,
            ...                            data_length=1000, user_name='xxx', password='xxx')

            >>> # [回测] 传入外部DataFrame
            >>> df = pd.read_csv('my_data.csv')
            >>> self.data = self.get_kline(df, duration_seconds=300)

            >>> # [回测] 保存获取的数据到本地（后续可用LocalDatas引用）
            >>> self.data = self.get_kline('SHFE.ag2506', data_length=2000, save='ag2506')

            >>> # [实盘] 实时获取K线
            >>> self.data = self.get_kline('SHFE.rb2410', duration_seconds=60, data_length=1000)
        """
        # -------------------------- 1. 优化/实盘缓存复用 --------------------------
        # 优化模式下策略实例在多轮参数迭代中复用，_first_start 在首轮结束后被设为 True；
        # 实盘模式下策略也会被反复 "重启"（start() 被多次触发）。
        # 此时 get_kline() 返回 None 是设计行为：__setattr__() 中会拦截 None 赋值，
        # 并保持 self.xxx 指向首次运行时的缓存 KLine（绕过 super().__setattr__()），
        # 从而跳过昂贵的重复数据获取，大幅提升优化迭代效率。
        # 详见 strategy.py 中 __setattr__() 第 1575-1579 行的缓存分支。
        if (self._isoptimize or self._is_live_trading) and self._first_start:
            return

        # -------------------------- 2. 参数预处理 --------------------------
        # 提前弹出 save 参数，避免污染后续传递给 KLine 和子获取函数的 kwargs
        save = kwargs.pop("save", None)

        # -------------------------- 3. 数据集容器初始化 --------------------------
        # 确保 _btklinedataset 存在（管理所有 KLine 实例的容器），
        # 部分场景下策略类可能未预先初始化，此处做防御性检查
        if not hasattr(self, '_btklinedataset'):
            from minibt.strategy.strategy import KLinesSet
            self._btklinedataset = KLinesSet()

        # 防御性初始化：确保 data 变量在所有路径下都有定义
        # （正常情况下 data 必定在实盘/回测分支中被赋值，此处仅为代码分析器友好）
        data: pd.DataFrame | None = None

        # -------------------------- 4. 生成KLine唯一标识ID --------------------------
        # btid 用于关联策略ID（_sid）、数据索引（num）、图表索引，确保多策略/多数据源不混淆
        id = self._btklinedataset.num
        btid = BtID(self._sid, id, id)
        # name 记录原始symbol（字符串形式），用于KLine命名和save保存的文件名
        name = None

        # -------------------------- 5. 模式分支：实盘 vs 回测 --------------------------
        if self._is_live_trading:
            # ========== 4a. 实盘模式：从TQSDK实时获取K线 ==========
            # 实盘对数据长度要求低（只需满足指标计算窗口），默认300根保证足够长度
            # 若用户传入有效值（>=10），则尊重用户指定的数据量
            data_length = data_length if (data_length is not None and data_length >= 10) else 300

            # 获取K线序列（TQSDK内部维护订阅，后续可通过tq_object更新）
            kline = self._api.get_kline_serial(
                symbol, duration_seconds, data_length)
            # 同步获取最新tick报价
            tick = self._api.get_tick_serial(symbol)
            data = kline.copy()
            # 统一时间格式（TQSDK返回的时间可能是日期类型，转为datetime）
            data.datetime = data.datetime.apply(time_to_datetime)
            # 保存原始TQ对象（供KLine运行时实时更新使用）
            kwargs.update({"tq_object": kline, "tq_tick": tick})

        else:
            # ========== 4b. 回测/优化模式：多数据源自动适配 ==========
            # 回测需要较大数据量支撑各类指标计算和策略预热（min_start_length），
            # 默认10000根，用户可传入更大或更小的值（最小值300）
            data_length = data_length if (data_length is not None and data_length >= 300) else 10000

            # ---- 4b.1 处理symbol为字符串的情况（期货/股票代码或文件路径） ----
            if isinstance(symbol, str) and symbol:
                name = symbol  # 记录原始symbol字符串，用于后续命名
                # 策略一：优先加载本地CSV文件（最快，不需要网络）
                symbol_path = os.path.join(
                    BASE_DIR, "data", "test", f"{symbol}.csv")
                if os.path.exists(symbol_path):
                    symbol = pd.read_csv(symbol_path)
                # 策略二：symbol本身是一个已存在的文件路径（如绝对路径）
                elif os.path.exists(symbol):
                    symbol = read_unknown_file(symbol)
                # 策略三：本地无数据，通过TQSDK在线获取期货K线（需账号密码或已初始化的_api）
                else:
                    user_name: str = kwargs.pop("user_name", "")
                    password: str = kwargs.pop("password", "")
                    if (user_name and password) or self._api:
                        # 若_api未初始化，用传入的账号密码创建连接
                        if not self._api:
                            with FilteredOutputRedirector():
                                from tqsdk import TqApi, TqAuth, TqKq
                            self._api = TqApi(
                                TqKq(), auth=TqAuth(user_name, password))
                        # 获取合约报价（含最小变动单位price_tick、合约乘数volume_multiple）
                        quote = self._api.get_quote(symbol)
                        # 获取K线序列，格式化时间
                        symbol = self._api.get_kline_serial(
                            symbol, duration_seconds, data_length,
                            kwargs.pop("chart_id", None),  # 天勤图表ID（可选）
                            kwargs.pop("adj_type", None))  # 复权类型（可选）
                        symbol.datetime = symbol.datetime.apply(
                            time_to_datetime)
                        # 直接注入合约信息到DataFrame列中
                        symbol["price_tick"] = quote.price_tick
                        symbol["volume_multiple"] = quote.volume_multiple
                # 若上述三种策略均未命中，symbol 仍为原始字符串，
                # 将在后续 4b.2~4b.5 的 elif 链中被进一步处理

            # ---- 4b.2 外部DataFrame（用户直接传入K线数据） ----
            if isinstance(symbol, pd.DataFrame):
                data = self.__check_and_add_fileds(symbol)

            # ---- 4b.3 股票数据（纯数字字符串代码，如'600000'） ----
            elif isinstance(symbol, str) and symbol[0].isdigit():
                data_source = kwargs.pop('data_source', 'akshare').lower()
                valid_sources = ['pytdx', 'baostock', 'akshare']
                assert data_source in valid_sources, (
                    f"数据源必须为{valid_sources}，当前为{data_source}"
                )
                # 动态调用对应的数据获取方法（如 _get_akshare_data）
                data = getattr(self, f"_get_{data_source}_data")(
                    symbol, duration_seconds, data_length, **kwargs)
                # 补充股票默认合约信息（与PyTDX逻辑保持一致）
                # 股票：price_tick=0.01（最小变动1分钱），volume_multiple=1.0（1股=1手乘数）
                data.add_info(
                    symbol=symbol,
                    duration=duration_seconds,
                    price_tick=1e-2,
                    volume_multiple=1.0,
                )

            # ---- 4b.4 期货数据：通过已初始化的TQSDK _api获取（无需重复输入账号密码） ----
            elif self._api:
                # 合约代码映射（支持简写转全称，如 'rb2410' → 'SHFE.rb2410'）
                symbol = self._tq_contracts_dict.get(symbol, symbol)
                data = self._api.get_kline_serial(
                    symbol, duration_seconds, data_length)
                data.datetime = data.datetime.apply(time_to_datetime)
                # 从报价补充合约信息
                quote = self._api.get_quote(symbol)
                data["price_tick"] = quote.price_tick
                data["volume_multiple"] = quote.volume_multiple

            # ---- 4b.5 兜底：从已加载的原始数据（_datas列表）中按名称匹配 ----
            elif len(self._datas) > 0:
                # 在 _datas 列表中查找 name 属性匹配的 DataFrame
                data = list(filter(lambda x: x.name == symbol, self._datas))[
                    0].copy()
                # 对匹配到的原始数据同样执行字段补充
                data = self.__check_and_add_fileds(data)

            # ---- 4b.6 所有途径均失败 ----
            else:
                raise ValueError(
                    f"无法获取数据：symbol={symbol}, "
                    f"无本地文件、无_api连接、无_datas数据。"
                    f"请检查symbol参数是否正确，或提供user_name/password连接TQSDK。"
                )

        # -------------------------- 6. 数据保存到本地CSV --------------------------
        if save and isinstance(data, pd.DataFrame):
            # 保存数据文件到 BASE_DIR/data/test/ 目录，
            # 并自动更新 data/utils.py 生成 LocalDatas 引用
            save_and_generate_utils(data, BASE_DIR, save, name)
            # 对保存的数据执行预处理（消除跳空、截取日期范围等）
            data = self.__process_data(data)

        # -------------------------- 7. 数据截取 --------------------------
        # 回测模式下取最新 data_length 根K线（实盘模式已在TQSDK调用中指定长度，无需二次截取）
        # 仅在 data_length >= 300 时才执行截取，避免对极小数据集误截断
        if isinstance(data_length, int) and data_length >= 300 and len(data) > data_length:
            data = data[-data_length:]

        # -------------------------- 8. 数据校验（三重断言防线） --------------------------
        assert isinstance(
            data, pd.DataFrame
        ), f"数据类型 {type(data).__name__}，非pd.DataFrame类型，请检查数据获取逻辑"
        assert not data.empty, (
            "数据不能为空，请检查数据源是否可用、symbol是否正确、"
            "网络是否连通、或是否在非交易时段获取数据"
        )
        assert set(data.columns).issuperset(
            set(FILED.ALL)
        ), (f"传入数据必须包含{FILED.ALL}, "
            f"实际数据列为:{list(data.columns)}，"
            f"缺失列:{set(FILED.ALL) - set(data.columns)}")

        # -------------------------- 9. 封装为KLine对象 --------------------------
        # 生成KLine的名称：有原始symbol则用 {symbol}_{周期} 格式，否则用 datas{plot_id} 格式
        name = f"{name}_{duration_seconds}" if name else f"datas{btid.plot_id}"
        return KLine(data, id=btid, sname=name, ind_name=name, **kwargs)

    def __check_and_add_fileds(self, data: pd.DataFrame, price_tick=1e-2, volume_multiple=1.) -> pd.DataFrame:
        """
        ## 检查并补充DataFrame的必要字段（确保符合KLine的格式要求）
        - 自动处理时间格式、补充合约信息（symbol、duration等），避免字段缺失

        Args:
            data (pd.DataFrame): 待处理的K线数据

        Returns:
            pd.DataFrame: 补充字段后的完整数据，包含['datetime', 'open', 'high', 'low', 'close', 'volume', 'symbol', 'duration', 'price_tick', 'volume_multiple']
        """

        # ==================== 时间字段优化处理 ====================
        # 1. 时间格式转换（支持float/str转datetime）
        datetime_col = data['datetime']

        # 检查是否已经是datetime类型
        if not pd.api.types.is_datetime64_any_dtype(datetime_col):
            # 优化：尝试多种时间格式批量转换，提高性能
            formats = ['%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S', '%Y-%m-%d', '%Y%m%d%H%M%S']
            converted = False
            
            for fmt in formats:
                try:
                    # 方法1：尝试使用pandas的to_datetime进行批量转换（性能最佳）
                    data['datetime'] = pd.to_datetime(
                        datetime_col, errors='coerce', utc=True, format=fmt)
                    
                    # 检查转换后是否有无效的NaT值
                    nat_count = data['datetime'].isna().sum()
                    if nat_count == 0:
                        converted = True
                        break
                except (ValueError, TypeError):
                    continue
            
            # 如果所有格式都失败，使用逐元素转换
            if not converted:
                data['datetime'] = datetime_col.apply(self._safe_time_conversion)

        # 验证时间字段是否单调递增（数据质量检查）
        if not data['datetime'].is_monotonic_increasing:
            print("警告: 时间序列不是单调递增的，将进行排序")
            # 优化：使用inplace=True参数，避免创建新的数据框
            data.sort_values('datetime', inplace=True)
            data.reset_index(drop=True, inplace=True)

        # ==================== 字段补充 ====================
        col = data.columns

        # 补充缺失字段（默认值适配股票/期货通用场景）
        # 优化：使用字典批量添加字段，减少数据框修改次数
        new_columns = {}
        if 'symbol' not in col:
            new_columns['symbol'] = f"symbol{self._btklinedataset.num}"
        if 'duration' not in col:
            new_columns['duration'] = get_cycle(data['datetime'])  # 自动计算周期（秒）
        if 'price_tick' not in col:
            new_columns['price_tick'] = price_tick  # 默认最小变动单位0.01
        if 'volume_multiple' not in col:
            new_columns['volume_multiple'] = volume_multiple  # 默认合约乘数1
        
        # 批量添加缺失字段
        if new_columns:
            data = data.assign(**new_columns)

        return data

    def _safe_time_conversion(self, time_val) -> pd.Timestamp:
        """
        ## 安全的时间值转换方法

        Args:
            time_val: 待转换的时间值

        Returns:
            pd.Timestamp: 转换后的时间戳

        Raises:
            ValueError: 当无法转换时抛出
        """
        try:
            # 尝试常见的多种时间格式
            if isinstance(time_val, (int, float)):
                # 处理时间戳（秒或毫秒）
                if time_val > 1e10:  # 毫秒时间戳
                    return pd.to_datetime(time_val, unit='ms')
                else:  # 秒时间戳
                    return pd.to_datetime(time_val, unit='s')
            elif isinstance(time_val, str):
                # 尝试常见日期时间格式
                for fmt in ['%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S',
                            '%Y-%m-%d', '%Y%m%d%H%M%S']:
                    try:
                        return pd.to_datetime(time_val, format=fmt)
                    except ValueError:
                        continue
                # 最后尝试pandas自动识别
                return pd.to_datetime(time_val)
            else:
                return pd.to_datetime(time_val)
        except Exception as e:
            raise ValueError(
                f"无法转换时间值: {time_val} (类型: {type(time_val)})") from e

    def _get_plot_datas(self):
        """
        ## 整理绘图所需数据（供前端或绘图工具使用）
        - 整合K线数据、指标数据、绘图配置，构建统一的绘图数据结构，支持止损线等特殊元素

        输出结构（_plot_datas）说明：
        ---
        [
            策略类名,
            [各合约的源数据对象],
            [各合约的指标绘图数据列表],
            [各合约是否为主图显示],
            [各合约的绘图配置（如颜色、线型）]
        ]
        """
        if self.config.isplot:  # 仅当配置开启绘图时执行
            # 1. 将止损线添加到指标集合（确保止损线可绘图）
            for _, value in self._btklinedataset.items():
                if value._klinesetting.isstop:
                    self._btindicatordataset.add_data(
                        value.stop_lines.sname, value.stop_lines)

            # 2. 初始化指标绘图数据容器（按合约数量分组）
            init_inds_datas = [[] for _ in range(self._btklinedataset.num)]
            _indicator_record = [[] for _ in range(self._btklinedataset.num)]

            # 3. 遍历所有指标，整理绘图数据（按合约分组）
            for k, v in self._btindicatordataset.items():
                # 获取指标的绘图数据（包含plot_id、是否显示、名称、数值等）
                plot_id, *datas = v._get_plot_datas(k)
                init_inds_datas[plot_id].append(datas)
                # 记录指标绘图配置（用于后续更新）
                _indicator_record[plot_id].append(
                    [*datas[:4], datas[8], datas[9]])

            # 4. 构建最终的绘图数据结构
            if self._strategy_replay:
                # 修复：在 replay 模式下，K 线数据截断后需要重置索引，使其从 0 开始
                # 这样交易信号的索引才能与 K 线数据对齐
                kline_datas = []
                for data in self._btklinedataset.values():
                    kline_df = data.kline_object[:self.min_start_length]
                    # 重置索引为 0 开始
                    if hasattr(kline_df, 'reset_index'):
                        kline_df = kline_df.reset_index(drop=True)
                    kline_datas.append(kline_df)
                
                # 修复：在 replay 模式下，交易结果数据也需要截断并重置索引
                if self._results:
                    self._results = [df.iloc[:self.min_start_length].reset_index(drop=True) for df in self._results]
            else:
                kline_datas = [
                    data.kline_object for data in self._btklinedataset.values()]
            self._plot_datas = [
                self.__class__.__name__,  # 策略类名
                kline_datas,  # 各合约源数据
                init_inds_datas,  # 各合约的指标绘图数据
                [data.get_indicator_kwargs()
                 for data in self._btklinedataset.values()]  # 各合约绘图配置
            ]

            # 5. 保存指标绘图配置记录
            self._indicator_record = _indicator_record
            self._btindicatordataset
            self._btklinedataset
            # 6. 保存回测数据到 JSON 文件（供 miniqt 回测结果展示使用）
            if self._light_chart:
                self._save_plot_datas_to_json()

    def _save_plot_datas_to_json(self):
        """保存回测绘图数据到 pickle 文件

        数据结构:
        {
            "strategy_name": "策略类名",
            "contracts": {
                "contract_0": {           # 或实际symbol名
                    "symbol": "...",
                    "kline": DataFrame,   # K线数据
                    "indicators": [...]   # 该合约的指标列表
                },
                "contract_1": { ... },
            },
            "account": {...}
        }
        指标数据中 plot_id 为整数，从0开始，0=第1个合约的指标，1=第2个合约的指标

        注意：此方法在回测结束时调用，此时可能处于 FilteredOutputRedirector
        重定向上下文中，需要恢复原始 stdout 确保日志可见。
        """

        # 临时恢复标准输出，避免被 FilteredOutputRedirector 吞掉日志
        # _orig_stdout = sys.stdout if hasattr(sys, 'stdout') else None
        # _orig_stderr = sys.stderr if hasattr(sys, 'stderr') else None

        # def _log(msg):
        #     """安全打印：优先使用原始stdout，否则尝试当前stdout"""
        #     try:
        #         if _orig_stdout is not None and hasattr(_orig_stdout, 'write'):
        #             _orig_stdout.write(str(msg) + "\n")
        #             _orig_stdout.flush()
        #         else:
        #             print(msg)
        #     except Exception:
        #         try:
        #             print(msg)
        #         except Exception:
        #             pass

        # 获取策略基类所在目录
        base_dir = os.path.dirname(os.path.abspath(__file__))
        pickle_path = os.path.join(base_dir, "backtest_result.pkl")

        # 构建数据结构：以合约字典形式组织
        plot_datas = {
            "strategy_name": str(self.__class__.__name__),
            "contracts": {}  # {合约key: {kline, indicators, ...}}
        }

        # 获取 K 线数据
        # 统一使用 KLine 对象（而非原始 DataFrame），确保后续代码能正确访问
        # pandas_object、symbol 等属性，避免在 replay 模式下因类型不一致导致异常
        kline_datas = list(self._btklinedataset.values())

        # 遍历每个合约的数据，构建以合约索引为key的字典
        contract_keys = []  # 保持顺序的合约key列表
        for i, kline_data in enumerate(kline_datas):
            symbol = str(kline_data.symbol) if hasattr(kline_data, 'symbol') else f"contract_{i}"
            contract_key = symbol  # 使用symbol作为key，保证唯一性

            contract_info = {
                "symbol": symbol,
                "index": i,  # 合约索引（与plot_id对应）
                "kline": None,
                "indicators": []
            }

            # 获取 K 线 DataFrame
            if hasattr(kline_data, 'pandas_object'):
                df = kline_data.pandas_object.copy()
            elif hasattr(kline_data, 'df'):
                df = kline_data.df.copy()
            else:
                df = pd.DataFrame(kline_data)

            # 转换 datetime 列为时间戳（纳秒级，与 _init_chart_data 实时模式一致）
            if 'datetime' in df.columns:
                df["time"] = df['datetime'].apply(time_to_ns_timestamp) + 8 * 3.6e12  # +8小时北京时间偏移


            # 只保留需要的列
            cols = [c for c in FILED.TALL if c in df.columns]
            contract_info["kline"] = df[cols].reset_index(drop=True)

            plot_datas["contracts"][contract_key] = contract_info
            contract_keys.append(contract_key)

        #_log(f"📊 plot_datas 结构: strategy={plot_datas['strategy_name']}, contracts={list(plot_datas['contracts'].keys())}")

        # 添加账户统计信息
        if hasattr(self, '_account') and self._account:
            plot_datas["account"] = {
                "cash": getattr(self._account, 'cash', 0),
                "balance": getattr(self._account, 'balance', 0),
                "total_profit": getattr(self._account, 'total_profit', 0),
                "net": getattr(self._account, 'net', 0),
                "total_commission": getattr(self._account, 'total_commission', 0),
            }
        # 添加权益曲线数据（供miniqt回测图表使用）
        if hasattr(self, 'profits') and self.profits is not None:
            try:
                profits_series = self.profits
                if hasattr(profits_series, 'index'):
                    if hasattr(profits_series.index, 'astype'):
                        times = profits_series.index.astype(str).tolist()
                    else:
                        times = [str(t) for t in profits_series.index]
                    values = profits_series.values.tolist() if hasattr(profits_series, 'values') else list(profits_series)
                    cummax = __import__('pandas').Series(values).cummax()
                    drawdowns = ((__import__('pandas').Series(values) - cummax) / cummax.replace(0, 1) * 100).tolist()
                    plot_datas["equity"] = {
                        "times": times,
                        "values": values,
                        "drawdowns": drawdowns,
                    }
            except Exception:
                pass



        # 按合约分组存储指标数据（plot_id 即合约索引）
        init_inds_datas = [[] for _ in range(self._btklinedataset.num)]

        for k, v in self._btindicatordataset.items():
            # 获取指标的绘图数据，plot_id 表示该指标属于哪个合约
            plot_id, isplot, name, lines, _lines, ind_names, overlaps, \
                categorys, indicators, doubles, _ind_plotinfo, span, _signal = v._get_plot_datas(k)

            ind_plotinfo = dict(_ind_plotinfo) if _ind_plotinfo else {}

            ind_data = {
                "isplot": isplot,
                "name": name,
                "lines": lines,
                "_lines": _lines,
                "ind_names": ind_names,
                "overlaps": overlaps,
                "categorys": categorys,
                "indicators": indicators,
                "doubles": doubles,
                "plotinfo": ind_plotinfo,
                "span": span,
                "signal": _signal
            }

            init_inds_datas[plot_id].append(ind_data)

        # 将指标数据按 plot_id 归入对应合约
        for plot_id, inds in enumerate(init_inds_datas):
            if plot_id < len(contract_keys):
                contract_key = contract_keys[plot_id]
                plot_datas["contracts"][contract_key]["indicators"] = inds
                #_log(f"📎 合约 [{contract_key}] 指标数: {len(inds)}")
                # 打印每个指标的原始数据结构
                for idx, ind in enumerate(inds):
                    ind_indicators = ind.get("indicators")
                    ind_type = type(ind_indicators).__name__
                    ind_shape = getattr(ind_indicators, 'shape', len(ind_indicators) if hasattr(ind_indicators, '__len__') else 'N/A')
                    #_log(f"   [{idx}] name={ind.get('name')}, _lines={ind.get('_lines')}, doubles={ind.get('doubles')}")
                    #_log(f"       indicators type={ind_type}, shape={ind_shape}")

        #_log(f"📦 准备保存回测数据到: {pickle_path}")
        #_log(f"📈 合约数量: {len(plot_datas['contracts'])}")

        # 将数据递归转换为 JSON 可序列化格式（根治 StringDtype 等 pickle 兼容性问题）
        # import numpy as np
        import json

        def _to_json_safe(obj):
            """递归将数据转换为纯 Python 类型，确保 JSON 可序列化。
            DataFrame → 嵌套 dict，numpy 数组 → list。
            """
            if isinstance(obj, pd.DataFrame):
                return {
                    '__type__': 'DataFrame',
                    'columns': list(obj.columns),
                    'data': {col: _to_json_safe(obj[col].to_numpy()) if hasattr(obj[col], 'to_numpy') else list(obj[col])
                             for col in obj.columns},
                }
            elif isinstance(obj, pd.Series):
                return {
                    '__type__': 'Series',
                    'data': _to_json_safe(obj.to_numpy()) if hasattr(obj, 'to_numpy') else list(obj),
                }
            elif isinstance(obj, (np.integer,)):
                return int(obj)
            elif isinstance(obj, (np.floating,)):
                return float(obj)
            elif isinstance(obj, np.ndarray):
                return obj.tolist()
            elif isinstance(obj, np.bool_):
                return bool(obj)
            elif isinstance(obj, dict):
                return {str(k): _to_json_safe(v) for k, v in obj.items()}
            elif isinstance(obj, (list, tuple)):
                return type(obj)(_to_json_safe(v) for v in obj)
            elif obj is None or isinstance(obj, (bool, int, float, str)):
                return obj
            else:
                # 其它不可序列化对象 → 转为字符串
                try:
                    return str(obj)
                except Exception:
                    return repr(obj)

        json_data = _to_json_safe(plot_datas)
        pickle_path = pickle_path.replace('.pkl', '.json')

        try:
            with open(pickle_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, ensure_ascii=False, indent=2)
            #_log(f"✅ 回测数据已成功保存到: {pickle_path}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            #_log(f"❌ 保存回测数据失败: {e}")

    # def __process_backtest_iteration(self, x: Any = 0):
    #     """
    #     ## 回测循环的迭代方法（逐根K线处理）
    #     - 处理止损逻辑、调用策略核心逻辑（step）、更新账户历史，是回测的核心循环单元

    #     Args:
    #         x (Any, optional): 迭代参数（无实际作用，适配map调用）. Defaults to 0.
    #     """
    #     # 优化：减少属性访问开销，直接使用局部变量
    #     btklinedataset = self._btklinedataset
    #     btindicatordataset = self._btindicatordataset
    #     account = self._account
    #     data_patching = self._data_patching
        
    #     self._btindex += 1
    #     if data_patching:
    #         btklinedataset.update_values()
    #         btindicatordataset.update_values()
    #     self.step()
    #     # 每次迭代检查 self._isstop，因为止损可能在 step() 中被设置
    #     if self._isstop:
    #         # 优化：使用for循环替代列表推导式，减少函数调用开销
    #         for data in btklinedataset.values():
    #             if data._klinesetting.isstop:
    #                 data.stop.update()
    #     account.update_history()
        
    def _strategy_replay_iteration(self,*args,**kwds) -> Strategy:
        self._btindex += 1
        return self

    def _execute_live_trading(self,*args,**kwds) -> Strategy:
        """
        ## 实盘交易的主方法（实时行情驱动）
        - 初始化实盘数据、处理RL动作（若启用）、调用策略核心逻辑（step），适配TQSDK实时推送

        ### 实盘与回测的关键区别：
        - 数据实时更新（从TQSDK推送获取）
        - 交易通过TargetPosTask实现（确保仓位准确）
        - 无回测索引迭代，依赖行情变化触发
        """
        self._in_next_context = False
        # 3. 实时更新K线数据（从TQSDK同步最新数据）
        [kline._inplace_values()
            for kline in self._btklinedataset.values()]
        # 4. 策略初始化（重新计算指标，适应实时数据）
        self._strategy_init()
        # 5. 标记初始化完成
        self._in_next_context = True
        # 6. RL模式：获取智能体动作（无梯度计算，避免性能消耗）
        if self.rl:
            with self.th.no_grad():
                rl_action = self.actor(
                    self.th.as_tensor(self.next_state, dtype=self.th.float32).unsqueeze(0)
                ).cpu().numpy()[0]
                self.step(rl_action)
        else:
        # 7. 非RL模式：调用策略核心逻辑
            self.step()
        return self

    def __process_rl_backtest_iteration(self, x):
        """
        ## RL模式下的回测迭代方法（逐根K线处理）
        - 从RL智能体（actor）获取动作，调用策略核心逻辑，更新状态与账户历史

        Args:
            x (Any): 迭代参数（无实际作用，适配map调用）
        """
        # 1. 更新回测索引
        self._btindex += 1
        # 2. 转换当前状态为torch张量（适配actor输入）
        tensor_state = self.th.as_tensor(
            self._state, dtype=self.th.float32, device=self.device).unsqueeze(0)
        # 3. 获取actor的动作（无梯度计算，回测模式）
        tensor_action = self.actor(tensor_state)
        action = tensor_action.detach().cpu().numpy()[0]
        # 4. 调用策略核心逻辑，更新状态
        self._state, *_ = self.step(action)
        # 5. 更新账户历史记录
        self._account.update_history()

    def _execute_core_trading_loop(self):
        """
        ## 回测主方法（统一调度回测流程）
        - 根据是否启用RL模式，调用对应的迭代方法，完成回测后执行收尾工作（止损、结果分析）
        """
        self._indicators_names = list(self._btindicatordataset.keys())
        # 1. 标记回测初始化完成
        self._in_next_context = True
        
        # 优化：预计算max_length，减少属性访问开销
        max_length = self._btklinedataset.max_length
        start_index = self._btindex
        end_index = max_length - 1

        # 2. RL模式回测（调用__process_rl_backtest_iteration迭代）
        if self.rl:
            # 无梯度模式（回测时禁用梯度计算，提升性能）
            with self.th.no_grad():
                # 优化：使用for循环替代list(map(...))，减少函数调用开销
                for i in range(start_index, end_index):
                    self.__process_rl_backtest_iteration(i)

        # 3. 普通模式回测（调用__process_backtest_iteration迭代）
        else:
            if self._isbacktrader_form_signals:
                self.__signal_results = self._bt_from_signals_func()
            else:
                # 优化：减少属性访问开销，直接使用局部变量
                btklinedataset = self._btklinedataset
                btindicatordataset = self._btindicatordataset
                account = self._account
                data_patching = self._data_patching
                # 优化：使用for循环替代list(map(...))，减少函数调用开销
                for i in range(start_index, end_index):
                    self._btindex += 1
                    if data_patching:
                        btklinedataset.update_values()
                        btindicatordataset.update_values()
                    self.step()
                    # 每次迭代检查 self._isstop，因为止损可能在 step() 中被设置
                    if self._isstop:
                        # 优化：使用for循环替代列表推导式，减少函数调用开销
                        for data in btklinedataset.values():
                            if data._klinesetting.isstop:
                                data.stop.update()
                    account.update_history()

        # 4. 回测收尾：执行策略停止逻辑、获取结果、初始化分析工具
        self.stop()           # 策略停止钩子（子类重写，如平仓、释放资源）
        self._get_result()    # 获取回测结果（从账户历史提取）
        self._qs_init()       # 初始化QuantStats分析（计算指标、绘图）
        # 5. 标记回测结束，重置初始化状态
        self._in_next_context = False
        
    def bt_from_signals(self,
                        entries:IndSeries | list[IndSeries],
                        exits:IndSeries | list[IndSeries],
                        size=1.0,
                        size_type=SizeType.Amount,
                        commission=1.0,
                        com_type=CommissionType.Fixed,
                        slip_point=0.0,
                        prices:IndSeries| list[IndSeries] | None=None,
                        sl_stop=0.0,
                        tp_stop=0.0,
                        stop_mode=0,
                        sl_trail=0,
                        sl_callback=None,
                        tp_callback=None,
                        sl_callback_args=(),
                        tp_callback_args=(),
                        max_hold_bars=0,)->partial:
        from ..indicators import IndSeries
        
        if isinstance(entries, IndSeries):
            entries = [entries,]
        if isinstance(exits, IndSeries):
            exits = [exits,]
        assert len(entries) == len(exits), "entries和exits必须具有相同的长度"
        assert all(isinstance(k, IndSeries) for k in entries), "entries必须是IndSeries或IndSeries列表"
        assert all(isinstance(k, IndSeries) for k in exits), "exits必须是IndSeries或IndSeries列表"
        kline = [signal.kline for signal in entries]
        
        # 转换为正确的numpy二维数组格式
        close = np.column_stack([k.close.values for k in kline])
        margin_rate = [float(k.margin_rate) for k in kline]
        price_tick = [float(k.price_tick) for k in kline]
        volume_multiple = [float(k.volume_multiple) for k in kline]
        entries = np.column_stack([k.values for k in entries])
        init_cash = float(self.account.cash)
        exits = np.column_stack([k.values for k in exits])
        min_start_length = self.min_start_length
        
        # 处理prices参数
        if prices is not None:
            if isinstance(prices, IndSeries):
                prices = [prices,]
            assert len(prices) == len(entries), "prices和entries必须具有相同的长度"
            assert all(isinstance(k, IndSeries) for k in prices), "prices必须是IndSeries或IndSeries列表"
            prices = np.column_stack([k.values for k in prices])
        
        # 确保数据是二维数组
        if close.ndim == 1:
            close = close.reshape(-1, 1)
        if entries.ndim == 1:
            entries = entries.reshape(-1, 1)
        if exits.ndim == 1:
            exits = exits.reshape(-1, 1)
        if prices is not None:
            prices = prices.reshape(-1, 1)
            
        from ..cython_functions.backtrader_from_signals import from_signals
        self._isbacktrader_form_signals = True
        
        # 提取 numba @cfunc 回调函数的 C 地址，并保持引用防止 GC
        sl_cb_addr = 0
        tp_cb_addr = 0
        if sl_callback is not None:
            self._sl_callback_obj = sl_callback  # 保持 @cfunc 对象存活
            sl_cb_addr = sl_callback.address
        if tp_callback is not None:
            self._tp_callback_obj = tp_callback
            tp_cb_addr = tp_callback.address
        
        self._bt_from_signals_func = partial(from_signals,
                                            close=close,
                                            entries=entries,
                                            exits=exits,
                                            size=size,
                                            size_type=size_type,
                                            margin_rate=margin_rate,
                                            price_tick=price_tick,
                                            volume_multiple=volume_multiple,
                                            prices=prices,
                                            min_start_length=min_start_length,
                                            init_cash=init_cash,
                                            commission=commission,
                                            com_type=com_type,
                                            slip_point=slip_point,
                                            sl_stop=sl_stop,
                                            tp_stop=tp_stop,
                                            stop_mode=stop_mode,
                                            sl_trail=sl_trail,
                                            sl_callback_addr=sl_cb_addr,
                                            tp_callback_addr=tp_cb_addr,
                                            sl_callback_args=sl_callback_args,
                                            tp_callback_args=tp_callback_args,
                                            max_hold_bars=max_hold_bars)
        return self._bt_from_signals_func
        
    @staticmethod
    def _to_ha(data: pd.DataFrame, isha: bool):
        """
        ## 将K线数据转换为HA（Heikin-Ashi，黑金）K线（静态方法）
        ### HA K线平滑价格波动，突出趋势，计算公式：
        - HA开盘价 = (前一根HA开盘价 + 前一根HA收盘价) / 2
        - HA收盘价 = (当前开盘价 + 当前最高价 + 当前最低价 + 当前收盘价) / 4
        - HA最高价 = max(当前最高价, HA开盘价, HA收盘价)
        - HA最低价 = min(当前最低价, HA开盘价, HA收盘价)

        Args:
            data (pd.DataFrame): 原始K线数据（需包含OHLC字段）
            isha (bool): 是否转换为HA K线（True=转换，False=不转换）

        Returns:
            pd.DataFrame: 转换后的K线数据（HA或原始）
        """
        if isha:
            df = data.ta.ha()
            # 替换原始OHLC字段为HA K线
            data.loc[:, FILED.OHLC] = df.values
        return data

    @staticmethod
    def _to_lr(data: pd.DataFrame, islr: int):
        """
        ## 将K线数据转换为线性回归K线（静态方法）
        - 线性回归K线通过线性回归模型平滑价格，突出趋势方向，适用于趋势跟踪策略

        Args:
            data (pd.DataFrame): 原始K线数据（需包含OHLC字段）
            islr (int): 线性回归窗口长度（>1时转换，否则不转换）

        Returns:
            pd.DataFrame: 转换后的K线数据（线性回归或原始）
        """
        if isinstance(islr, int) and islr > 1:
            # 调用ta库计算线性回归K线
            df = data.ta.Linear_Regression_Candles(length=islr)
            # 替换原始OHLC字段为线性回归K线
            data.loc[:, FILED.OHLC] = df.values
        return data

    def _get_result(self):
        """
        ## 获取回测结果（从账户历史记录提取）
        - 支持参数优化模式（避免重复获取），返回所有Broker的历史数据列表

        Returns:
            list[pd.DataFrame]: 回测结果列表，每个元素对应一个Broker的历史数据（含权益、仓位、盈亏等）
        """
        if self._isbacktrader_form_signals:
            cols = ["total_profit", "positions",
                "sizes", "float_profits", "cum_profits","total_fee"]
            self._results = [pd.DataFrame(result, columns=cols) for result in self.__signal_results]
        elif self._isoptimize or (not self._results):
            # 从账户获取历史记录（每个Broker对应一个DataFrame）
            self._results = self._account._get_history_results()
        return self._results

    @property
    def plot(self) -> None:
        """
        ## 绘图开关属性,无返回值
        - 可统一设置所有指标plot属性,赋值类型为bool
        """
        ...

    @plot.setter
    def plot(self, value):
        """
        ## 设置所有指标的绘图开关（兼容旧接口，实际调用isplot逻辑）

        Args:
            value (bool): 绘图开关（True=显示指标，False=隐藏指标）
        """
        self._btindicatordataset.isplot = bool(value)
        
    @property
    def btklinedataset(self) -> KLinesSet[str, KLine] | dict[str, KLine] | None:
        """
        ## 获取当前合约数据集（兼容旧接口，实际调用btklinedataset）
        - 可统一获取所有合约数据集，赋值类型为BtkLineDataset
        """
        if hasattr(self, "_btklinedataset"):
            return self._btklinedataset
    
    @property
    def btindicatordataset(self) -> BtIndicatorDataSet[str, KLine] | dict[str, Line | IndSeries | IndFrame] | None:
        """
        ## 获取当前合约指标集（兼容旧接口，实际调用btindicatordataset）
        - 可统一获取所有合约指标集，赋值类型为BtIndicatorDataSet
        """
        if hasattr(self, "_btindicatordataset"):
            return self._btindicatordataset

    def buy(self,
            data: KLine | None = None,
            size: int = 1,
            exectype: OrderType | None = None,
            price: float | None= None,
            valid: datetime | timedelta | int | None = None,
            stop: BtStop | None = None,
            ) -> Order | float:
        """
        ### 买入开仓/加仓接口（统一封装回测与实盘逻辑）
        - 支持指定合约、手数、止损参数，自动校验手数有效性
        - 订单成交价格规则：
        1. OrderType.Close：在当前K线收盘价交易
        2. OrderType.Market：在下根K线开盘价交易
        3. Limit/Stop等其他类型：保持原有委托逻辑不变

        Args:
            data (KLine, optional): 目标合约数据
            size (int): 买入手数
            exectype (OrderType): 订单类型，默认收盘价成交(OrderType.Close)
                - Close: 当前K线收盘价成交
                - Market: 下根K线开盘价成交
                - Limit/Stop: 自定义委托价格成交
            price (float): 委托价格（限价单/止损单需要）
            valid (Union[datetime.datetime, datetime.timedelta, float]): 订单有效期
            stop (BtStop): 停止器设置

        Returns:
            Order | float: 创建的订单对象（回测模式）或浮动盈亏（实盘模式）
        """
        # 区分实盘与回测，调用对应逻辑
        if self._is_live_trading:
            return self._buy_live_trading(data=data, size=size, stop=stop)
        if exectype is None:
            exectype = self._default_order_type
        return self._buy_back_trading(data, size, exectype, price, valid, stop)

    def _buy_back_trading(self, data: KLine | None = None, size: int = 1,
                          exectype: OrderType = OrderType.Close,
                          price: float | None= None,
                          valid: datetime | timedelta |  int | None = None,
                          stop:BtStop | None =None) -> Order:
        """
        ## 回测模式的买入逻辑（内部方法，供buy调用）
        - 校验手数、设置止损（若指定）、调用Broker更新仓位，返回交易盈亏

        Args:
            data (KLine, optional): 目标合约数据
            size: (int) 买入手数
            exectype (OrderType): 订单类型（Market, Limit, Stop等）
            price (float): 委托价格（限价单/止损单需要）
            valid (Union[datetime.datetime, datetime.timedelta, float]): 订单有效期
            stop (BtStop): 停止器设置

        Kwargs:
        ---
        bar (int): 1
            - 针对OrderType.Market，OrderType.Close这两种订单类型
            - 大于或等于0的整数
            - 等于0 ：即当前bar的市场价或收盘价成交
            - 等于1 ：即后一根bar的市场价或收盘价成交

        Returns:
            Order: 创建的订单对象
        """
        # 参数校验
        size = int(size)
        assert size > 0, '手数为不少于0的正整数'

        # 默认使用主合约数据
        if data is None:
            data = self._btklinedataset.default_kline

        # 创建买入订单
        order = data._broker.buy(
            size,
            exectype=exectype,
            price=price,
            valid=valid,
            stop=stop,
        )

        return order

    def _buy_live_trading(self, data: KLine | None= None, size: int = 1, stop: BtStop | None =None) -> float:
        """
        ## 实盘模式的买入逻辑（内部方法，供buy调用）
        - 通过TQSDK的TargetPosTask设置目标仓位，实现买入开仓/加仓，返回当前浮动盈亏

        Args:
            data (KLine, optional): 目标合约数据. Defaults to None.
            size (int): 买入手数（相对于当前仓位的增量）. Defaults to 1.
            stop (Any, optional): 止损参数（预留，实盘止损需单独处理）. Defaults to None.
            **kwargs: 额外参数（预留）.

        Returns:
            float: 当前合约的浮动盈亏

        Raises:
            AssertionError: size非正整数时触发
        """
        size = int(size)
        assert size > 0, '手数为不少于0的正整数'
        if data is None:
            data = self._btklinedataset.default_kline
        tqobj = self._tqobjs[data.symbol]
        # 获取当前仓位对象
        position = tqobj.Position
        pos = position.pos
        # 计算目标仓位（当前仓位 + 买入手数）
        size += pos
        # 获取当前浮动盈亏
        profit = position.float_profit
        # 通过TargetPosTask设置目标仓位（实盘核心逻辑）
        tqobj.TargetPosTask.set_target_volume(size)
        # 等待仓位更新（最多10秒）
        deadline = _time.time() + 10
        while position.pos != size or _time.time() > deadline:
            self.api.wait_update(1)
            profit = position.float_profit
        if not pos and position.pos:
            data._cooldown_entry_time = data.datetime[-1]
        # 返回浮动盈亏
        return profit

    def sell(self, data: KLine | None = None, size: int = 1,
             exectype: OrderType | None = None,
             price: float | None = None,
             valid: datetime | timedelta | float | None = None,
             stop:BtStop | None =None,
             ) -> Order | float:
        """
        ### 卖出平仓/开空接口（统一封装回测与实盘逻辑）
        - 支持指定合约、手数、止损参数，自动校验手数有效性
        - 订单成交价格规则：
        1. OrderType.Close：在当前K线收盘价交易
        2. OrderType.Market：在下根K线开盘价交易
        3. Limit/Stop等其他类型：保持原有委托逻辑不变

        Args:
            data (KLine, optional): 目标合约数据
            size (int): 卖出手数
            exectype (OrderType): 订单类型，默认收盘价成交(OrderType.Close)
                - Close: 当前K线收盘价成交
                - Market: 下根K线开盘价成交
                - Limit/Stop: 自定义委托价格成交
            price (float): 委托价格（限价单/止损单需要）
            valid (Union[datetime.datetime, datetime.timedelta, float]): 订单有效期
            stop (BtStop): 停止器设置

        Returns:
            Order | float: 创建的订单对象（回测模式）或浮动盈亏（实盘模式）
        """
        # 区分实盘与回测，调用对应逻辑
        if self._is_live_trading:
            return self._sell_live_trading(data=data, size=size, stop=stop)
        if exectype is None:
            exectype = self._default_order_type
        return self._sell_back_trading(data, size, exectype, price, valid, stop)

    def _sell_back_trading(self, data: KLine | None = None, size: int = 1,
                           exectype: OrderType = OrderType.Close,
                           price: float | None = None,
                           valid: datetime | timedelta | int | None = None,
                           stop: BtStop | None = None) -> Order:
        """
        ## 回测模式的卖出逻辑（内部方法，供sell调用）
        - 校验手数、设置止损（若指定）、调用Broker更新仓位，返回交易盈亏

        Args:
            data (KLine, optional): 目标合约数据
            size: (int) 买入手数
            exectype (OrderType): 订单类型（Market, Limit, Stop等）
            price (float): 委托价格（限价单/止损单需要）
            valid (Union[datetime.datetime, datetime.timedelta, float]): 订单有效期
            stop (BtStop): 停止器设置

        Returns:
            Order: 创建的订单对象
        """
        # 参数校验
        size = int(size)
        assert size > 0, '手数为不少于0的正整数'

        # 默认使用主合约数据
        if data is None:
            data = self._btklinedataset.default_kline
        # 创建买入订单
        order = data._broker.sell(
            size,
            exectype=exectype,
            price=price,
            valid=valid,
            stop=stop
        )

        return order

    def _sell_live_trading(self, data: KLine | None = None, size: int = 1, stop: BtStop | None =None, **kwargs):
        """
        ## 实盘模式的卖出逻辑（内部方法，供sell调用）
        - 通过TQSDK的TargetPosTask设置目标仓位，实现卖出平仓/开空，返回当前浮动盈亏

        Args:
            data (KLine, optional): 目标合约数据. Defaults to None.
            size (int): 卖出手数（相对于当前仓位的减量）. Defaults to 1.
            stop (Any, optional): 止损参数（预留）. Defaults to None.
            **kwargs: 额外参数（预留）.

        Returns:
            float: 当前合约的浮动盈亏

        Raises:
            AssertionError: size非正整数时触发
        """
        size = int(size)
        assert size > 0, '手数为不少于0的正整数'
        if data is None:
            data = self._btklinedataset.default_kline
        tqobj = self._tqobjs[data.symbol]
        # 获取当前仓位对象
        position = tqobj.Position
        pos = position.pos
        # 计算目标仓位（当前仓位 - 卖出手数，负数表示空头）
        size -= pos
        # 获取当前浮动盈亏
        profit = position.float_profit
        # 通过TargetPosTask设置目标仓位（负仓位表示空头）
        tqobj.TargetPosTask.set_target_volume(-size)
        # 等待仓位更新（最多10秒）
        deadline = _time.time() + 10
        while position.pos != -size or _time.time() > deadline:
            self.api.wait_update(1)
            profit = position.float_profit
        if not pos and position.pos:
            data._cooldown_entry_time = data.datetime[-1]
        # 返回浮动盈亏
        return profit

    def set_target_size(self, data: KLine | None = None, size: int = 0) -> None:
        """
        ## 设置目标仓位接口（统一封装回测与实盘逻辑）
        - 直接指定最终仓位手数，自动计算仓位差并执行交易（开仓/平仓/加仓/减仓）

        Args:
            data (KLine, optional): 目标合约数据（默认使用默认合约）. Defaults to None.
            size (int): 目标仓位手数（正数=多头，负数=空头，0=平仓）. Defaults to 0.
        """
        # 区分实盘与回测，调用对应逻辑
        if self._is_live_trading:
            return self._set_target_size_live_trading(data, size)
        return self._set_target_size_back_trading(data, size)

    def _set_target_size_back_trading(self, data: KLine | None = None, size: int = 0):
        """
        ## 回测模式的目标仓位逻辑（内部方法，供set_target_size调用）
        - 计算当前仓位与目标仓位的差值，调用Broker执行对应的交易

        Args:
            data (KLine, optional): 目标合约数据. Defaults to None.
            size (int): 目标仓位手数. Defaults to 0.
        """
        # 默认使用主合约数据
        if data is None:
            data = self._btklinedataset.default_kline
        # 转换为整数手数
        size = int(size)
        # 获取当前仓位
        pre_pos = data.position.pos
        # 计算仓位差（目标仓位 - 当前仓位）
        diff_pos = size - pre_pos
        # 仓位差非零时执行交易
        if diff_pos:
            # 调用Broker执行交易（diff_pos>0=买入，<0=卖出）
            data._broker.update(abs(diff_pos), diff_pos > 0)

    def _set_target_size_live_trading(self, data: KLine | None = None, size: int = 0):
        """
        ## 实盘模式的目标仓位逻辑（内部方法，供set_target_size调用）
        - 通过TQSDK的TargetPosTask直接设置目标仓位，自动处理交易细节

        Args:
            data (KLine, optional): 目标合约数据. Defaults to None.
            size (int): 目标仓位手数. Defaults to 0.
        """
        # 默认使用主合约数据
        if data is None:
            data = self._btklinedataset.default_kline
        tqobj = self._tqobjs[data.symbol]
        # 转换为整数手数
        size = int(size)
        # 目标仓位与当前仓位不同时执行
        if size != tqobj.Position.pos:
            # 通过TargetPosTask设置目标仓位
            tqobj.TargetPosTask.set_target_volume(size)
            # 等待仓位更新（最多10秒）
            deadline = _time.time() + 10
            while tqobj.Position.pos != size or _time.time() > deadline:
                self.api.wait_update(1)

    def insert_order(self, kline: KLine | None = None, direction: OrderSide | int=OrderSide.Buy, offset: str = "", volume: int = 0,
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
        if not self._is_live_trading:
            return None
        if account is None:
            account = self._account
        if kline is None:
            kline = self._btklinedataset.default_kline
        symbol=kline.symbol
        if direction==OrderSide.Sell:
            direction="SELL"
        else:
            direction="BUY"
        return self.api.insert_order(symbol, direction, offset, volume, limit_price, advanced, order_id, account)
    
    def in_cooldown(self, data: KLine | None = None, cooldown_seconds: float | int = 0.0,cooldown_bars: int | None = None) -> bool | None:
        """
        检查是否在冷却中（开仓后 N 秒内禁止任何交易操作（包括平仓））
        Args:
            data: KLine 数据对象
            cooldown_seconds: 冷却时间（秒）
            cooldown_bars: 冷却K线数
        Returns:
            bool: 是否在冷却中
            
        Note:
            如果 cooldown_seconds 和 cooldown_bars 都为 None，则默认 cooldown_bars 优先级高。
        """
        if not self._is_live_trading:
            return False
        if pd.isna(data._cooldown_entry_time):
            return False
        if not cooldown_seconds and not cooldown_bars :
            return False
        if not isinstance(cooldown_seconds,(float,int)):
            cooldown_seconds = 0.0
        if isinstance(cooldown_bars,int):
            cooldown_seconds = cooldown_bars * data.cycle
        now = data.datetime[-1]
        elapsed = (now - data._cooldown_entry_time).total_seconds()
        if elapsed < cooldown_seconds:
            return True
        data._cooldown_entry_time = pd.NaT  # 冷却结束
        return False
    # ------------------------------
    # 量化分析（QuantStats）相关方法
    # ------------------------------

    def _qs_init(self) -> Stats:
        """
        ## 初始化量化分析工具（QuantStats）
        - 计算账户净值、初始化Stats（统计指标）和QSPlots（绘图），支持参数优化模式

        Args:
            isop (bool, optional): 是否为参数优化模式. Defaults to False.

        Returns:
            Stats: 初始化后的统计分析对象
        """
        # 重置回测指标缓存（每次回测重新计算）
        self._backtest_metrics = None

        if self._isbacktrader_form_signals:
            result = self._results[0]
            self.profits = result["total_profit"]
            self._account._balance=self.profits.iloc[-1]
            self._account._total_commission=result["total_fee"].iloc[-1]
            self._account._total_profit=result["total_profit"].iloc[-1]
            # 2. 判断收益是否有效（排除所有收益相同的情况）
            #state = len(self.profits.unique()) != 1.
        else:
            # 1. 获取回测收益序列（从账户历史提取）
            self.profits = self._account.get_profits()
        # 2. 判断收益是否有效（排除所有收益相同的情况）
        state = len(self.profits.unique()) != 1.
        # 3. 根据收益有效性调整配置（避免无意义分析）
        if self.config.print_account:
            self.config.print_account = state  # 收益无效时不打印账户信息
        if self.config.profit_plot:
            self.config.profit_plot = state  # 收益无效时不绘制收益曲线
        

        # 4. 处理净值序列（计算收益率，用于后续分析）
        # index = self._btklinedataset.date_index  # 获取K线时间索引
        # print(len(index), len(self.profits))
        # self.profits.index = index  # 对齐时间索引

        # 4. 处理净值序列（计算收益率，用于后续分析）
        index = self._btklinedataset.date_index  # 获取K线时间索引
        # print(f"时间索引长度: {len(index)}, 收益序列长度: {len(self.profits)}")
        
        # **关键修复：处理min_start_length**
        start_idx = getattr(self.config, 'min_start_length', 0)

        # 如果收益序列长度不等于时间索引长度，进行对齐
        if len(self.profits) != len(index):
            if start_idx > 0:
                # 情况1：收益序列包含完整长度（包含前start_idx个0值）
                if len(self.profits) == len(index):
                    # 保持原样，因为收益序列已经对齐
                    pass
                # 情况2：收益序列不包含前start_idx个数据点
                elif len(self.profits) == len(index) - start_idx:
                    # 截取时间索引的后半部分
                    index = index[start_idx:]
                else:
                    # 长度不匹配，截取到较短的长度
                    min_len = min(len(self.profits), len(index))
                    if start_idx > 0:
                        # 如果设置了min_start_length，从start_idx开始截取
                        index = index[start_idx:start_idx + min_len]
                    else:
                        index = index[:min_len]
                    self.profits = self.profits.iloc[:min_len]
            else:
                # 没有min_start_length，直接截取到相同长度
                min_len = min(len(self.profits), len(index))
                index = index[:min_len]
                self.profits = self.profits.iloc[:min_len]

        # 对齐时间索引
        self.profits.index = index
        
        self._net_worth = self.profits.pct_change()[1:]  # 计算日度收益率（跳过首行NaN）
        # 5. 初始化QSPlots（绘图对象，非优化模式）
        if not self._isoptimize:
            from .qs_plots import QSPlots
            self._qs_plots = QSPlots(
                self.profits, index=index, name='net_worth')

        # 6. 初始化Stats（统计分析对象，计算夏普比率、最大回撤等）
        self._stats = Stats(
            self.profits,
            index=index,
            name='profit',
            available=self.config.value  # 初始资金
        )
        return self._stats

    def _get_broker_trade_stats(self):
        """## 从 Broker 历史记录提取真实交易统计（仓位状态机）

        遍历每个 Broker 的 history_queue，通过仓位状态机检测完整开平仓周期：
        - FLAT → 有仓位：记录入场累计盈亏
        - 有仓位 → FLAT：平仓，通过 cum_profits 差值判断盈亏
        - 反向翻转（多↔空）：先平旧仓再开新仓

        ### Returns:
            tuple: (wins, losses, win_rate, trade_count)
            - wins/losses: 盈利/亏损交易次数
            - win_rate: 胜率（小数，如 0.35 表示 35%）
            - trade_count: 总交易次数
        """
        wins = 0
        losses = 0

        if not hasattr(self, '_account'):
            return 0, 0, 0.0, 0

        acc = self._account
        if not hasattr(acc, 'brokers'):
            return 0, 0, 0.0, 0

        for broker in acc.brokers:
            if not hasattr(broker, 'history_queue'):
                continue

            try:
                history = list(broker.history_queue.queue)
            except Exception:
                continue

            if not history or len(history) < 2:
                continue

            prev_pos = 0
            entry_cum_profit = 0.0

            for record in history:
                if len(record) < 6:
                    continue
                pos = int(record[2])
                cum_profit = float(record[4])

                if prev_pos == 0 and pos != 0:
                    # 开仓
                    entry_cum_profit = cum_profit
                elif prev_pos != 0 and pos == 0:
                    # 平仓
                    trade_pnl = cum_profit - entry_cum_profit
                    if trade_pnl > 0:
                        wins += 1
                    else:
                        losses += 1
                elif prev_pos != 0 and pos != 0 and prev_pos != pos:
                    # 反手：先平旧仓
                    trade_pnl = cum_profit - entry_cum_profit
                    if trade_pnl > 0:
                        wins += 1
                    else:
                        losses += 1
                    # 开新仓
                    entry_cum_profit = cum_profit

                prev_pos = pos

        trade_count = wins + losses
        win_rate = wins / trade_count if trade_count > 0 else 0.0
        return wins, losses, win_rate, trade_count

    def _compute_backtest_metrics(self) -> dict | None:
        """## 统一计算回测核心统计指标，结果缓存到 self._backtest_metrics
        - 所有指标基于 quantstats.stats 计算，与 minibt logger.py 保持一致
        - 仅计算一次，后续通过 self._backtest_metrics 直接读取

        ### Returns:
            dict 或 None: 回测指标字典，收益无效时为 None

        ### 缓存字典键值说明：
        - final_return:   回测期间总收益（绝对值）
        - commission:      回测期间总手续费
        - compounded:      累计收益率（复利，如 1.2 表示 20%）
        - sharpe:          年化夏普比率
        - max_drawdown:    最大回撤（负值）
        - value_at_risk:   风险值 VaR（95% 置信区间）
        - risk_return_ratio: 风险收益比
        - profit_factor:   盈亏比（总盈利/总亏损）
        - profit_ratio:    收益比率（平均盈利/平均亏损）
        - win_rate:        胜率（基于 Broker 真实交易统计）
        - wins:            盈利交易次数（基于 Broker 真实交易统计）
        - losses:          亏损交易次数（基于 Broker 真实交易统计）
        - avg_return:      单次交易平均收益
        - avg_win:         单次盈利平均金额
        - avg_loss:        单次亏损平均金额
        - trade_count:     总交易次数（wins + losses）
        - formatted:       格式化列表，供 pprint 直接使用
        """
        # 已缓存则直接返回
        if hasattr(self, '_backtest_metrics') and self._backtest_metrics is not None:
            return self._backtest_metrics

        if not hasattr(self, "profits") or self.profits is None:
            self._backtest_metrics = None
            return None

        # 计算单次收益（原始收益序列差分，首行 NaN 替换为 0）
        profits = pd.Series(self.profits).diff()
        profits.iloc[0] = 0.
        # 收益率序列（用于风险指标计算）
        returns = self._net_worth

        # 仅当收益存在波动时计算指标（排除所有收益相同的无效情况）
        if len(profits.unique()) <= 1:
            self._backtest_metrics = None
            return None

        # 1. 收益相关指标
        final_return = profits.sum()
        comm = self._account._total_commission
        compounded = qs_stats.comp(returns)

        # 2. 风险相关指标
        sharpe = qs_stats.sharpe(returns)
        max_dd = qs_stats.max_drawdown(returns)
        value_at_risk = qs_stats.value_at_risk(returns)
        risk_return_ratio = qs_stats.risk_return_ratio(returns)

        # 3. 交易质量指标
        profit_factor = qs_stats.profit_factor(returns)
        profit_ratio = qs_stats.profit_ratio(returns)

        # 4. 交易频率指标（从 Broker 历史记录提取真实交易，非日收益差分）
        broker_wins, broker_losses, broker_win_rate, broker_trade_count = \
            self._get_broker_trade_stats()

        # 5. 收益幅度指标
        avg_return = qs_stats.avg_return(profits)
        avg_win = qs_stats.avg_win(profits)
        avg_loss = qs_stats.avg_loss(profits)

        # 缓存到策略实例
        self._backtest_metrics = {
            'final_return': final_return,
            'commission': comm,
            'compounded': compounded,
            'sharpe': sharpe,
            'max_drawdown': max_dd,
            'value_at_risk': value_at_risk,
            'risk_return_ratio': risk_return_ratio,
            'profit_factor': profit_factor,
            'profit_ratio': profit_ratio,
            'win_rate': broker_win_rate,
            'wins': broker_wins,
            'losses': broker_losses,
            'avg_return': avg_return,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'trade_count': broker_trade_count,
            'formatted': [
                ("final return", final_return, "{:.2f}"),
                ("commission", comm, "{:.2f}"),
                ("compounded", compounded, "{:.2%}"),
                ("sharpe", sharpe, "{:.4f}"),
                ("risk", value_at_risk, "{:.4f}"),
                ("risk/return", risk_return_ratio, "{:.4f}"),
                ("max_drawdown", abs(max_dd), "{:.4%}"),
                ("profit_factor", profit_factor, "{:.4f}"),
                ("profit_ratio", profit_ratio, "{:.4f}"),
                ("win_rate", broker_win_rate, "{:.4%}"),
                ("wins", broker_wins, "{:d}"),
                ("losses", broker_losses, "{:d}"),
                ("avg_return", avg_return, "{:.6f}"),
                ("avg_win", avg_win, "{:.6f}"),
                ("avg_loss", avg_loss, "{:.6f}"),
            ],
        }
        return self._backtest_metrics

    @property
    def richprint(self):
        self.logger.print_strategy(self)
        
    @property
    def _print_account(self):
        self.logger.print_account(self._account,self._isbacktrader_form_signals)

    @property
    def pprint(self):
        """## 格式化打印回测核心统计指标（使用统一缓存的 _backtest_metrics）"""
        metrics = self._compute_backtest_metrics()
        if metrics is not None and 'formatted' in metrics:
            print(format_3col_report(metrics['formatted'], self.__class__.__name__))

    def _qs_reports(self, report_cwd="", report_name="", show=False, **kwargs):
        """
        ## 生成QuantStats详细分析报告（HTML格式）
        - 包含收益曲线、回撤分析、交易分布等可视化图表，支持本地保存与自动打开
        """
        import quantstats as qs
        # 1. 构建报告保存路径
        if not report_cwd or not isinstance(report_cwd, str):
            report_cwd = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "analysis_reports")

        # 确保目录存在
        os.makedirs(report_cwd, exist_ok=True)

        # 如果不在 Jupyter 环境或者 show=False，则保存到 reports 子目录
        report_dir = os.path.join(report_cwd, "reports")
        os.makedirs(report_dir, exist_ok=True)

        # 2. 生成报告文件名
        filename = f"{report_name if report_name else self.__class__.__name__}_analysis_report.html"
        output = os.path.normpath(os.path.join(report_dir, filename))

        # 3. 强制使用亮色主题，避免与Jupyter环境冲突
        kwargs.setdefault('style', 'light')

        try:
            # 生成HTML报告
            qs.reports.html(
                self._net_worth,
                output=output,
                download_filename=output,
                **kwargs
            )

        except Exception as e:
            print(f"生成QuantStats报告失败: {str(e)}")
            return

        # 4. 打印保存路径提示，自动打开报告（若show=True）
        if show:
            # 检查是否在 Jupyter 环境中
            try:
                from IPython import get_ipython
                _shell = get_ipython()
                IS_JUPYTER_NOTEBOOK = _shell is not None and _shell.__class__.__name__ == 'ZMQInteractiveShell'
            except (ImportError, AttributeError):
                IS_JUPYTER_NOTEBOOK = False
            print(f"| Analysis reports save to: {output}")
            # 添加主题切换功能
            self._add_theme_switcher(output)
            try:
                if IS_JUPYTER_NOTEBOOK:
                    # 读取并显示报告内容
                    self._display_html_in_notebook(output)
                else:
                    # 非 Jupyter 环境，直接在浏览器中打开
                    import webbrowser
                    webbrowser.open(f"file://{output}")
            except Exception as e:
                print(f"| 显示报告失败：{str(e)}，请手动打开文件：{output}")

    def _add_theme_switcher(self, output_path):
        """## 为HTML报告添加主题切换功能"""
        try:
            with open(output_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # 在head标签结束前添加CSS样式
            css_insert = """
            <style>
                :root {
                    --primary-bg: #f8f9fa;
                    --text-color: #212529;
                    --card-bg: #ffffff;
                    --border-color: #dee2e6;
                    --header-bg: #e9ecef;
                    --accent-color: #0d6efd;
                }

                .dark-theme {
                    --primary-bg: #121212;
                    --text-color: #e0e0e0;
                    --card-bg: #1e1e1e;
                    --border-color: #424242;
                    --header-bg: #2d2d2d;
                    --accent-color: #3d85c6;
                }

                body {
                    background-color: var(--primary-bg);
                    color: var(--text-color);
                    transition: all 0.3s ease;
                }

                .theme-switch-container {
                    position: fixed;
                    top: 10px;
                    right: 10px;
                    z-index: 1000;
                }

                .theme-switch {
                    position: relative;
                    display: inline-block;
                    width: 60px;
                    height: 30px;
                }

                .theme-switch input {
                    opacity: 0;
                    width: 0;
                    height: 0;
                }

                .slider {
                    position: absolute;
                    cursor: pointer;
                    top: 0;
                    left: 0;
                    right: 0;
                    bottom: 0;
                    background-color: #ccc;
                    transition: .4s;
                    border-radius: 30px;
                }

                .slider:before {
                    position: absolute;
                    content: "";
                    height: 22px;
                    width: 22px;
                    left: 4px;
                    bottom: 4px;
                    background-color: white;
                    transition: .4s;
                    border-radius: 50%;
                }

                input:checked + .slider {
                    background-color: var(--accent-color);
                }

                input:checked + .slider:before {
                    transform: translateX(30px);
                }
                
                /* 确保图表背景在亮色和暗色主题下都正确显示 */
                .js-plotly-plot .plotly, .plot-container {
                    background-color: var(--card-bg) !important;
                }
                
                .main-svg {
                    background-color: var(--card-bg) !important;
                }
            </style>
            """

            # 在body标签开始后添加主题切换按钮
            theme_switch_html = """
                <div class="theme-switch-container">
                    <label class="theme-switch">
                        <input type="checkbox" id="theme-toggle">
                        <span class="slider"></span>
                    </label>
                </div>
                """

            # 在body标签结束前添加JavaScript
            js_insert = """
            <script>
                const toggleSwitch = document.querySelector('#theme-toggle');

                // 检查本地存储中的主题偏好
                const currentTheme = localStorage.getItem('theme') || 'light';
                if (currentTheme === 'dark') {
                    document.body.classList.add('dark-theme');
                    toggleSwitch.checked = true;
                }

                // 切换主题函数
                function switchTheme(e) {
                    if (e.target.checked) {
                        document.body.classList.add('dark-theme');
                        localStorage.setItem('theme', 'dark');
                    } else {
                        document.body.classList.remove('dark-theme');
                        localStorage.setItem('theme', 'light');
                    }
                    
                    // 触发resize事件以确保Plotly图表重新渲染
                    setTimeout(() => {
                        window.dispatchEvent(new Event('resize'));
                    }, 100);
                }

                toggleSwitch.addEventListener('change', switchTheme);
            </script>
            """

            # 插入CSS到head
            if '</head>' in content:
                content = content.replace('</head>', css_insert + '</head>')

            # 插入主题切换按钮到body开始处
            if '<body>' in content:
                content = content.replace(
                    '<body>', '<body>' + theme_switch_html)

            # 插入JavaScript到body结束前
            if '</body>' in content:
                content = content.replace('</body>', js_insert + '</body>')

            # 写回文件
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(content)

        except Exception as e:
            print(f"添加主题切换功能失败: {str(e)}")

    def _display_html_in_notebook(self, file_path, height=600):
        """## 在Jupyter Notebook中显示HTML内容"""
        from IPython.display import display, HTML
        import base64

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                html_content = f.read()

            filename = os.path.basename(file_path)
            abs_path = os.path.abspath(file_path)
            # 将内容编码为base64
            content_b64 = base64.b64encode(
                html_content.encode('utf-8')).decode('utf-8')
            data_uri = f"data:text/html;base64,{content_b64}"

            display(HTML(f"""
                <p>📊  分析报告:</p>
                <!-- 浏览器渲染打开（默认显示页面效果） -->
                <a href="{file_path}" target="_blank" style="display: inline-block; margin-right: 15px; padding: 8px 12px; background: #4CAF50; color: white; text-decoration: none; border-radius: 4px;">
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

            display(HTML(f"""
            <div style="margin: 20px 0; border: 1px solid #ddd; border-radius: 5px; padding: 10px;">
                <h4 style="margin-top: 0;">报告预览</h4>
                <iframe 
                    srcdoc='{escaped_html}' 
                    width="100%" 
                    height="{height}" 
                    frameborder="0"
                    style="border: 1px solid #eee; border-radius: 3px;"
                ></iframe>
                <p style="font-size: 12px; color: #666; margin: 10px 0 0 0;">
                    如果图表未正常显示，请点击上方链接查看完整报告
                </p>
            </div>
            """))
        except Exception as e:
            print(f"显示报告内容失败: {str(e)}")
            # 如果 IFrame 失败，尝试直接显示 HTML
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    html_content = f.read()
                display(HTML(html_content))
            except Exception as e2:
                print(f"直接显示 HTML 也失败: {str(e2)}")

    def _reset_optimization_state(self):
        """
        ## 参数优化模式下重置策略状态（用于多组参数循环测试）
        - 避免前一组参数的回测状态影响下一组，确保每组参数独立测试

        ### 重置内容：
        1. 回测索引（_btindex）：重置为-1或最小开始长度-1
        2. 账户状态：重置账户历史、仓位、权益等（从指定索引开始）
        3. 回测结果：清空结果列表，避免数据残留
        """
        # 1. 重置回测索引
        self._btindex = -1
        # 若配置了最小开始长度，索引设为最小长度-1（跳过初始化阶段）
        if self.config.min_start_length > 0:
            strat_index = self.config.min_start_length - 1
            self._btindex = strat_index
        # 2. 重置账户状态（从当前索引+1开始，避免重复计算）
        self._account.reset(self._btindex + 1)

        # 3. 清空回测结果列表
        self._results = []

    def reset(self) -> tuple[np.ndarray, dict]:
        """
        ## 强化学习（RL）环境重置接口（抽象方法，需子类重写）
        - 用于RL训练/推理时重置环境状态，返回初始观测值与环境信息

        :Returns:
            tuple[np.ndarray, dict]: 
                - 初始观测值数组（形状：[state_dim]）
                - 环境信息字典（如初始资金、合约信息等）

        ### 注意：
        子类需根据具体策略逻辑实现，例如：
        1. 重置账户状态（资金、仓位）
        2. 重新加载K线数据
        3. 计算初始观测特征（指标、账户状态）
        """
        ...

    def step(self) -> tuple[np.ndarray, float, bool, bool, dict]:
        """
        ## 策略核心交易逻辑接口（抽象方法，需子类重写）
        - 回测/实盘模式下逐根K线调用，RL模式下接收动作并执行交易

        :Returns:
            tuple[np.ndarray, float, bool, bool, dict]: 
                - 新观测值数组（RL模式返回，非RL模式可忽略）
                - 本次步骤收益（用于RL奖励计算）
                - terminal（是否达到自然终止条件，如数据结束）
                - truncated（是否达到截断条件，如最大回撤）
                - 信息字典（如交易记录、仓位变化等）

        ### 核心职责：
        - 1. 读取当前K线/指标数据
        - 2. 执行交易逻辑（开仓/平仓/加仓/减仓）
        - 3. 计算收益与风险指标
        - 4. 判断是否终止（回测结束、触发止损等）
        """
        ...

    def start(self) -> None:
        """
        ## 策略初始化钩子（抽象方法，需子类重写）
        - 在策略__init__初始化后、回测/实盘循环（next/step）前调用

        ### 核心用途：
        - 1. 初始化指标（如MA、RSI、MACD等）
        - 2. 设置初始参数（手续费、滑点、止损条件）
        - 3. 加载历史数据（若未提前加载）
        - 4. 初始化日志/监控工具

        ### 注意：
        该方法仅调用一次，用于策略启动前的准备工作
        """
        ...

    def stop(self) -> None:
        """
        ## 策略终止钩子（抽象方法，需子类重写）
        - 在回测结束、实盘停止或异常退出时调用

        ### 核心用途：
        - 1. 执行收尾操作（如平仓所有仓位、释放资源）
        - 2. 保存回测结果/模型参数
        - 3. 生成最终统计报告
        - 4. 关闭数据库/API连接（若未自动关闭）

        ### 注意：
        - 该方法确保策略优雅退出，避免资源泄露或数据丢失
        """
        ...

    def _close(self) -> None:
        """
        ## 关闭策略依赖的外部连接（API、数据库）
        - 避免资源泄露，在策略停止后自动调用

        ### 处理对象：
        - 1. TQSDK API连接（_api）：若存在close方法则调用
        """
        # 关闭TQSDK API连接（实盘模式常用）
        if hasattr(self._api, 'close'):
            self._api.close()

    def _strategy_init(self) -> None:
        """
        ## 策略实际初始化函数（内部调用，可被子类重写）
        - 用于回测/实盘模式下的动态初始化，支持多次调用（如实盘实时更新）

        ### 核心职责：
        - 1. 重新计算指标（适应实时数据更新）
        - 2. 刷新账户状态（实盘模式下同步最新仓位/权益）
        - 3. 重置临时变量（如交易计数器、止损条件）

        ### 区别于start方法：
        - start：仅调用一次，用于启动前的静态初始化
        - _strategy_init：可多次调用，用于动态更新状态（如实盘每根K线前）
        """
        ...

    @property
    def api(self) -> TqApi | None:
        """
        ## 获取天勤TQApi实例（属性接口）
        - 仅在实盘模式或从TQSDK获取数据时有效，用于操作TQSDK相关功能

        Returns:
            Optional[TqApi]: TQApi实例（未初始化则返回None）
        """
        """天勤API"""
        return self._api

    @property
    def sid(self) -> int:
        """
        ## 获取策略ID（属性接口）
        - 用于多策略并行运行时的唯一标识，避免资源冲突

        Returns:
            int: 策略ID（非负整数）
        """
        """策略id"""
        return self._sid

    def __process_data(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        ## 预处理K线数据（内部方法，供get_data调用）
        - 整合周期计算、跳空消除、数据截取等功能，确保数据格式统一

        Args:
            data (pd.DataFrame): 原始K线数据（需包含datetime、OHLCV字段）

        Returns:
            pd.DataFrame: 预处理后的标准K线数据

        ### 处理流程：
        - 1. 计算K线周期（秒）：优先从duration字段获取，否则自动计算
        - 2. 消除跳空：处理非交易时间导致的价格断层（如期货10:15-10:30休盘）
        - 3. 数据截取：按配置截取指定长度/时间范围的数据
        - 4. 时间过滤：按配置过滤指定时间段的数据（如仅保留开盘后1小时）
        """
        col = data.columns

        # 1. 计算K线周期（秒）
        if 'duration' in col:
            # 优先从字段获取周期（已存在则直接使用）
            cycle = int(data.duration.iloc[0])
        else:
            # 自动计算周期：基于时间差的最小值
            time_delta = pd.Series(
                data.datetime).diff().bfill()  # 计算相邻时间差，前向填充首行
            try:
                # 处理numpy datetime64类型（转换为秒）
                cycle = int(min(time_delta.unique().tolist()) / 1e9)
            except:
                # 处理datetime.timedelta类型（提取秒数）
                td = [x.seconds for x in time_delta.unique().tolist()]
                cycle = int(min(td))
            # 添加周期字段到数据
            data['duration'] = cycle

        # 2. 消除跳空（如期货休盘导致的价格断层）
        data = self.__clear_gap(data, cycle)
        # 3. 按长度/比例截取数据（如取后1000根K线）
        data = self.__get_data_segment(data)
        # 4. 按日期范围截取数据（如2023-01-01至2023-12-31）
        data = self.__get_datetime_segment(data)
        # 5. 按每日时间范围过滤数据（如仅保留9:30-11:30）
        data = self.__get_time_segment(data)

        return data

    def __clear_gap(self, data: pd.DataFrame, cycle: int) -> pd.DataFrame:
        """
        ## 消除K线跳空（内部方法，供__process_data调用）
        - 处理非交易时间导致的价格断层（如期货10:15-10:30休盘），使价格序列连续

        Args:
            data (pd.DataFrame): 原始K线数据
            cycle (int): K线周期（秒）

        Returns:
            pd.DataFrame: 消除跳空后的K线数据

        ### 核心逻辑：
        1. 识别跳空位置：时间差不等于周期且不等于周期+休盘时间（900秒=15分钟）的K线
        2. 计算跳空幅度：跳空K线的开盘价与前一根收盘价的差值
        3. 修正价格：跳空位置后的所有OHLC价格减去跳空幅度，消除断层
        """
        try:
            # 仅当配置开启跳空消除时执行
            if self.config.clear_gap:
                # 1. 计算相邻K线的时间差（处理numpy datetime64类型）
                time_delta = pd.Series(data.datetime.values).diff().bfill()
                # 2. 定义正常时间差列表：周期 + 休盘周期（10:15-10:30休盘900秒）
                cycle_ls = [
                    timedelta(seconds=cycle),
                    timedelta(seconds=900 + cycle)  # 包含休盘的正常时间差
                ]
                # 3. 识别跳空K线索引（时间差不在正常列表中）
                _gap_index = ~time_delta.isin(cycle_ls)
                _gap_index = np.argwhere(
                    _gap_index.values).flatten()  # 转换为索引数组
                _gap_index = np.array(
                    list(filter(lambda x: x > 0, _gap_index)))  # 过滤首行（无前置K线）

                # 4. 若存在跳空，修正价格
                if _gap_index.size > 0:
                    # 计算跳空幅度：跳空K线开盘价 - 前一根收盘价
                    _gap_diff = data.open.values[_gap_index] - \
                        data.close.values[_gap_index - 1]
                    # 修正跳空位置后的所有OHLC价格（减去跳空幅度）
                    for id, ix in enumerate(_gap_index):
                        data.loc[ix:, FILED.OHLC] = data.loc[ix:, FILED.OHLC].apply(
                            lambda x: x - _gap_diff[id]
                        )
        except Exception:
            # 捕获所有异常，避免预处理失败影响后续流程
            pass
        return data

    def __get_data_segment(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        ## 按长度/比例截取K线数据（内部方法，供__process_data调用）
        - 支持按百分比、固定长度、区间截取，满足回测数据量控制需求

        Args:
            data (pd.DataFrame): 原始K线数据
            segment (Union[float, int, Iterable]): 截取参数（来自config.data_segments）

        Returns:
            pd.DataFrame: 截取后的K线数据

        ### 支持的截取模式：
        1. 浮点数（-1 < segment < 1）：按百分比截取（如0.5=前50%，-0.5=后50%）
        2. 整数（1 < abs(segment) < 数据长度）：按固定长度截取（如1000=前1000根）
        3. 二元组（float/int）：按区间截取（如(0.2,0.8)=中间60%，(100,1000)=第100-1000根）
        """
        segment = self.config.data_segments
        length = data.shape[0]  # 数据总行数

        # 1. 浮点数模式：按百分比截取
        if isinstance(segment, float) and -1. < segment < 1.:
            if segment > 0.:
                # 正数：截取前N%（如0.5=前50%）
                data = data.iloc[:int(length * segment) + 1]
            else:
                # 负数：截取后N%（如-0.5=后50%）
                data = data.iloc[int(length * segment):]
                data.reset_index(drop=True, inplace=True)  # 重置索引

        # 2. 整数模式：按固定长度截取
        elif isinstance(segment, int):
            if 1 < abs(segment) < length:
                if segment > 0:
                    # 正数：截取前N根（如1000=前1000根）
                    data = data.iloc[:segment]
                else:
                    # 负数：截取后N根（如-1000=后1000根）
                    data = data.iloc[segment:]
                    data.reset_index(drop=True, inplace=True)

        # 3. 二元组模式：按区间截取
        elif isinstance(segment, Iterable) and len(segment) == 2:
            # 3.1 百分比区间（如(0.2, 0.8)=中间60%）
            if all([isinstance(s, float) and 0. < s < 1. for s in segment]):
                segment = list(sorted(segment))  # 确保区间有序（start <= stop）
                start = int(length * segment[0])
                stop = int(length * segment[1]) + 1
                data = data.iloc[start:stop]
                data.reset_index(drop=True, inplace=True)
            # 3.2 固定长度区间（如(100, 1000)=第100-1000根）
            elif all([isinstance(s, int) and 0 < s < length for s in segment]):
                segment = list(sorted(segment))
                data = data.iloc[segment[0]:segment[1]]
                data.reset_index(drop=True, inplace=True)

        return data

    def __get_datetime_segment(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        ## 按日期范围截取K线数据（内部方法，供__process_data调用）
        - 支持按开始/结束日期过滤数据，满足指定时间范围的回测需求

        Args:
            data (pd.DataFrame): 原始K线数据
            start (Union[str, datetime]): 开始日期（来自config.start_time）
            end (Union[str, datetime]): 结束日期（来自config.end_time）

        Returns:
            pd.DataFrame: 按日期过滤后的K线数据

        ### 核心逻辑：
        1. 日期格式统一：将字符串格式转换为datetime类型
        2. 按日期过滤：保留在[start, end]范围内的数据
        3. 重置索引：确保过滤后索引连续
        """
        try:
            # 1. 按开始日期过滤
            start = self.config.start_time
            if start:
                # 统一日期格式（字符串转datetime）
                if not isinstance(start, datetime):
                    start = time_to_datetime(start)
                # 保留>=开始日期的数据
                data = data[data.datetime >= start]
                data.reset_index(drop=True, inplace=True)

            # 2. 按结束日期过滤
            end = self.config.end_time
            if end:
                # 统一日期格式
                if not isinstance(end, datetime):
                    end = time_to_datetime(end)
                # 保留<=结束日期的数据
                data = data[data.datetime <= end]
                data.reset_index(drop=True, inplace=True)
        except Exception:
            # 捕获所有异常，避免日期格式错误影响后续流程
            pass
        return data

    def __get_time_segment(self, data: pd.DataFrame) -> pd.DataFrame:
        """
        ## 按每日时间范围过滤K线数据（内部方法，供__process_data调用）
        - 支持过滤每日内的指定时间段（如排除午休时间），满足精细化回测需求

        Args:
            data (pd.DataFrame): 原始K线数据
            segment (Iterable): 时间过滤参数（来自config.time_segments）

        Returns:
            pd.DataFrame: 按每日时间过滤后的K线数据

        ### 支持的过滤模式：
        1. 整数二元组列表（如[(10,15), (10,30)]）：排除10:15-10:30的数据
        2. time对象列表（如[time(10,15), time(10,30)]）：同上，格式更明确
        """
        segment = self.config.time_segments
        try:
            # 仅当过滤参数为可迭代对象且长度>=2时执行
            if isinstance(segment, Iterable):
                length = len(segment)
                if length >= 2:
                    # 1. 整数二元组模式（如[(10,15), (10,30)]）
                    if all([isinstance(s, Iterable) and all([isinstance(_s, int) for _s in s]) for s in segment]):
                        # 按每两个元素一组，排除指定时间段
                        for i, j in list(zip(range(0, length, 2), range(1, length, 2))):
                            t1, t2 = segment[i], segment[j]
                            # 转换为time对象（时:分）
                            t1 = time(t1[0], t1[1])
                            t2 = time(t2[0], t2[1])
                            # 提取每日时间（忽略日期和微秒）
                            t = data.datetime.apply(
                                lambda x: x.time().replace(microsecond=0))
                            # 排除[t1, t2)区间的数据
                            data = data[~((t1 <= t) & (t < t2))]
                        data.reset_index(drop=True, inplace=True)

                    # 2. time对象模式（如[time(10,15), time(10,30)]）
                    elif all([isinstance(s, time) for s in segment]):
                        # 逻辑同上，直接使用time对象比较
                        for i, j in list(zip(range(0, length, 2), range(1, length, 2))):
                            t1, t2 = segment[i], segment[j]
                            t = data.datetime.apply(
                                lambda x: x.time().replace(microsecond=0))
                            data = data[~((t1 <= t) & (t < t2))]
                        data.reset_index(drop=True, inplace=True)
        except Exception:
            # 捕获所有异常，避免时间格式错误影响后续流程
            pass
        return data

    @property
    def toolbox(self) -> bool:
        """## lightweight_charts图表是否开启画图工具
        - 只适用于实时图表"""
        if not hasattr(self, "_toolbox"):
            self._toolbox = True
        return self._toolbox

    @toolbox.setter
    def toolbox(self, value):
        if not hasattr(self, "_toolbox"):
            self._toolbox = True
        self._toolbox = bool(value)

    @property
    def btindex(self) -> int:
        """
        ## 获取回测当前索引（属性接口）
        - 表示当前处理到的K线位置，从-1开始（未启动），逐根K线递增

        Returns:
            int: 回测当前索引
        """
        """### 索引"""
        return self._btindex

    @property
    def min_start_length(self) -> int:
        """
        ## 获取最小开始回测长度（属性接口）
        - 表示策略启动前需要的最小K线数量（用于指标初始化，如MA5需要5根K线）

        Returns:
            int: 最小开始回测长度（非负整数）
        """
        return self.config.min_start_length

    @min_start_length.setter
    def min_start_length(self, value) -> int:
        """
        ## 设置最小开始回测长度（属性接口）
        - 同时调整回测起始索引（设为value-1），避免在指标未初始化时执行交易

        Args:
            value (Union[int, float]): 最小开始回测长度（需>=0，自动转换为整数）

        ### 逻辑：
        - 若value>0，回测索引设为value-1（从第value根K线开始执行策略）
        - 仅在非实盘模式下生效（实盘无需提前初始化）
        """
        if isinstance(value, (int, float)) and value >= 0:
            value = int(value)
            # 更新配置中的最小开始长度
            self.config.min_start_length = value
            # 非实盘模式下调整回测起始索引
            if not self._is_live_trading:
                if value > 0:
                    self._btindex = value - 1

    @property
    def logger(self) -> Logger:
        """
        ## 获取策略专用的日志记录器实例（属性接口）

        ### 该日志记录器专为策略运行设计，提供以下核心功能：

        🎯 **交易操作日志**
        - 开多/开空、平多/平空等专业交易操作记录
        - 智能颜色编码：黄色(创建/开仓)、绿色(盈利平仓)、红色(亏损平仓)
        - 表情符号辅助识别，提升日志可读性

        💰 **账户信息展示**
        - 实时账户权益、可用现金、持仓详情
        - 自动计算收益率和交易统计
        - 美观的表格化展示

        📈 **策略性能分析**
        - 完整的回测报告生成
        - 收益、风险、交易质量多维度指标
        - 基于行业标准的性能评估

        🔧 **调试与监控**
        - 多级别日志控制(DEBUG/INFO/WARNING/ERROR等)
        - 函数性能监控装饰器
        - 详细的错误追踪和异常处理

        **使用示例:**
        ```python
        # 记录交易操作
        self.logger.open_long("策略A", "IF2406", datetime, price, quantity, fee, capital)
        self.logger.close_short("策略A", "IF2406", datetime, price, quantity, fee, pnl, capital)

        # 记录错误和警告
        self.logger.log_insufficient_cash(datetime, "资金不足详情")
        self.logger.log_trade_failed(datetime, "失败原因", "额外信息")

        # 其它输出
        self.logger.debug(message)     # 调试
        self.logger.info(message)      # 信息
        self.logger.warning(message)   # 警告
        self.logger.error(message)     # 错误
        self.logger.critical(message)  # 严重错误
        self.logger.success(message)   # 成功
        self.logger.exception(message) # 异常

        # 性能监控
        @self.logger.time_it
        def critical_function():
            pass

        # 打印账户和策略报告
        self.logger.print_account(account)
        self.logger.print_strategy(strategy)
        ```

        **核心优势:**
        - ✅ 专为交易场景优化的日志格式
        - ✅ 丰富的可视化展示和颜色编码
        - ✅ 完整的交易统计和性能分析
        - ✅ 灵活的日志级别控制
        - ✅ 文件日志和实时监控支持

        **注意事项:**
        - 日志级别默认为 INFO，可通过 set_log_level() 调整
        - 建议在策略初始化时配置日志参数
        - 文件日志功能需显式启用

        Returns:
            Logger: 策略专用的日志记录器实例，提供完整的交易日志功能

        📘 **文档参考**:
        - https://www.minibt.cn/minibt_basic/1.18minibt_transaction_log/
        - Logger: 日志记录器类的详细文档
        - LogLevel: 日志级别枚举定义
        """
        return self.Logger()

    @property
    def stats(self) -> Stats:
        """
        ## 获取回测统计分析对象（属性接口）
        - 包含夏普比率、最大回撤等核心指标的计算与存储

        Returns:
            Stats: 统计分析对象（未初始化则返回None）
        """
        """分析器"""
        return self._stats

    @property
    def qs_plots(self) -> QSPlots:
        """
        ## 获取QuantStats绘图对象（属性接口）
        - 用于生成收益曲线、回撤曲线等可视化图表

        Returns:
            QSPlots: 绘图对象（未初始化则返回None）
        """
        """qs中的plot"""
        return self._qs_plots

    @property
    def account(self) -> BtAccount | TqAccount:
        """
        ## 获取账户对象（属性接口）
        - 回测模式返回BtAccount，实盘模式返回TqAccount，统一封装账户操作

        Returns:
            Union[BtAccount, TqAccount]: 账户对象（未初始化则返回None）
        """
        """账户"""
        return self._account

    @property
    def plot_name(self) -> str:
        """
        ## 获取图表保存名称（属性接口）
        - 用于指定策略回测图表的保存文件名（如"MA_strategy_plot"）

        Returns:
            str: 图表保存名称（默认"plot"）
        """
        """图表保存名称"""
        return self._plot_name

    @plot_name.setter
    def plot_name(self, value: str):
        """
        ## 设置图表保存名称（属性接口）
        - 仅接受非空字符串，确保文件名有效

        Args:
            value (str): 新的图表保存名称
        """
        if isinstance(value, str) and value:
            self._plot_name = value

    @property
    def qs_reports_name(self) -> str:
        """
        ## 获取回测报告名称（属性接口）
        - 用于指定QuantStats HTML报告的保存文件名（如"MA_strategy_report"）

        Returns:
            str: 回测报告名称（默认"qs_reports"）
        """
        """回测报告名称"""
        return self._qs_reports_name

    @qs_reports_name.setter
    def qs_reports_name(self, value: str):
        """
        ## 设置回测报告名称（属性接口）
        - 仅接受非空字符串，确保文件名有效

        Args:
            value (str): 新的回测报告名称
        """
        if isinstance(value, str) and value:
            self._qs_reports_name = value

    @property
    def result(self) -> pd.DataFrame:
        """
        ## 获取首个合约的回测结果（属性接口）
        - 适用于单合约策略，返回第一个Broker的历史数据（含权益、仓位等）

        Returns:
            pd.DataFrame: 首个合约的回测结果
        """
        """回测结果"""
        return self._results[0]

    @property
    def results(self) -> list[pd.DataFrame]:
        """
        ## 获取所有合约的回测结果（属性接口）
        - 适用于多合约策略，返回每个Broker的历史数据列表

        Returns:
            list[pd.DataFrame]: 回测结果列表（每个元素对应一个合约）
        """
        """回测结果"""
        return self._results

    @property
    def tick_commission(self, value: float) -> list[float]:
        """
        ## 获取所有合约的按tick手续费（属性接口）
        - 按tick收费模式：每手手续费 = 波动1个tick的价值 × 倍数

        Args:
            value (float): 预留参数（兼容setter格式）

        Returns:
            list[float]: 各合约的tick手续费倍数列表（默认0.0）
        """
        """每手手续费为波动一个点的价值的倍数"""
        return [kline._broker.commission.get("tick_commission", 0.) for kline in self._btklinedataset.values()]

    @tick_commission.setter
    def tick_commission(self, value: float):
        """
        ## 设置所有合约的按tick手续费（属性接口）
        - 批量更新所有合约的tick手续费倍数，适用于统一调整手续费

        Args:
            value (Union[int, float]): tick手续费倍数（需>0，自动转换为float）
        """
        if isinstance(value, (float, int)) and value > 0.:
            # 遍历所有合约，更新tick手续费
            [kline._broker._setcommission(
                dict(tick_commission=float(value))
            ) for kline in self._btklinedataset.values()]

    @property
    def percent_commission(self, value: float) -> list[float]:
        """
        ## 获取所有合约的按比例手续费（属性接口）
        - 按比例收费模式：每手手续费 = 每手价值 × 百分比（如0.0001=0.01%）

        Args:
            value (float): 预留参数（兼容setter格式）

        Returns:
            list[float]: 各合约的比例手续费列表（默认0.0）
        """
        """每手手续费为每手价值的百分比"""
        return [kline._broker.cost_percent for kline in self._btklinedataset.values()]

    @percent_commission.setter
    def percent_commission(self, value: float):
        """
        ## 设置所有合约的按比例手续费（属性接口）
        - 批量更新所有合约的比例手续费，适用于统一调整手续费

        Args:
            value (Union[int, float]): 比例手续费（需>0，自动转换为float）
        """
        if isinstance(value, (float, int)) and value > 0.:
            # 遍历所有合约，更新比例手续费
            [kline._broker._setcommission(
                dict(percent_commission=float(value))
            ) for kline in self._btklinedataset.values()]

    @property
    def fixed_commission(self) -> list[float]:
        """
        ## 获取所有合约的固定手续费（属性接口）
        - 固定收费模式：每手手续费为固定金额（如5元/手）

        Returns:
            list[float]: 各合约的固定手续费列表（默认0.0）
        """
        """每手手续费为固定手续费"""
        return [kline._broker.cost_fixed for kline in self._btklinedataset.values()]

    @fixed_commission.setter
    def fixed_commission(self, value: float):
        """
        ## 设置所有合约的固定手续费（属性接口）
        - 批量更新所有合约的固定手续费，适用于统一调整手续费

        Args:
            value (Union[int, float]): 固定手续费（需>0，自动转换为float）
        """
        if isinstance(value, (float, int)) and value > 0.:
            # 遍历所有合约，更新固定手续费
            [kline._broker._setcommission(
                dict(fixed_commission=float(value))
            ) for kline in self._btklinedataset.values()]

    @property
    def slip_point(self) -> float:
        """
        ## 获取所有合约的滑点（属性接口）
        - 滑点：每次交易的价格偏差（如0.1表示每次交易偏差0.1个tick）

        Returns:
            list[float]: 各合约的滑点列表（默认0.0）
        """
        """每手手续费为固定手续费"""  # 注：原注释有误，实际为滑点
        return [kline._broker.slip_point for kline in self._btklinedataset.values()]

    @slip_point.setter
    def slip_point(self, value: float):
        """
        ## 设置所有合约的滑点（属性接口）
        - 批量更新所有合约的滑点，模拟实际交易中的价格偏差

        Args:
            value (Union[int, float]): 滑点（需>=0，自动转换为float）
        """
        if isinstance(value, (float, int)) and value >= 0.:
            # 遍历所有合约，更新滑点
            [kline._broker._setslippoint(value)
             for kline in self._btklinedataset.values()]

    @property
    def on_close(self) -> bool:
        """
        ## 获取成交价模式（属性接口）
        - 控制策略执行交易时使用的成交价，影响回测准确性

        Returns:
            bool: 
                - True：使用当前K线收盘价作为成交价（适用于收盘后决策）
                - False：使用下一根K线开盘价作为成交价（适用于盘中实时决策，避免未来函数）
        """
        """是否使用当前交易信号的收盘价作成交价,否为使用下一根K线的开盘价作成交价"""
        return self.config.on_close
    
    @on_close.setter
    def on_close(self, value: bool):
        """
        ## 设置成交价模式（属性接口）
        - 控制策略执行交易时使用的成交价，影响回测准确性

        Args:
            value (bool): 
                - True：使用当前K线收盘价作为成交价（适用于收盘后决策）
                - False：使用下一根K线开盘价作为成交价（适用于盘中实时决策，避免未来函数）
        """
        """是否使用当前交易信号的收盘价作成交价,否为使用下一根K线的开盘价作成交价"""
        self.config.on_close = bool(value)
        self._default_order_type = OrderType.Close if value else OrderType.Market

    @property
    def key(self) -> str:
        """
        ## 获取行情更新字段（属性接口）
        - 实盘模式下，用于判断行情是否更新（如"last_price"表示最新价更新时触发）

        Returns:
            str: 行情更新字段名称（来自config.key）
        """
        """行情更新字段"""
        return self.config.key

    @property
    def actor(self) -> Callable | None:
        """
        ## 获取强化学习（RL）智能体（actor）（属性接口）
        - 用于RL模式下的动作决策，返回actor网络的前向传播函数

        Returns:
            Callable: actor网络（输入状态，输出动作）
        """
        """第一个合约强化学习模型"""
        return self._actor

    @actor.setter
    def actor(self, value: Callable | None):
        """
        ## 设置强化学习（RL）智能体（actor）（属性接口）
        - 加载训练好的actor网络，用于RL推理或继续训练

        Args:
            value (Callable): 训练好的actor网络（需兼容输入输出格式）
        """
        """第一个合约强化学习模型"""
        self._actor = value

    @property
    def env(self):
        """
        ## 获取强化学习（RL）环境（属性接口）
        - 用于RL训练时的环境交互，包含状态转换、奖励计算等逻辑

        Returns:
            Any: RL环境对象（具体类型由子类实现）
        """
        """强化学习事件"""
        return self

    @env.setter
    def env(self, value):
        """
        ## 设置强化学习（RL）环境（属性接口）
        - 初始化或更新RL环境，用于训练或推理

        Args:
            value (Any): RL环境对象（需实现reset/step接口）
        """
        """强化学习事件"""
        # self._env = value
        pass

    @property
    def is_changing(self) -> bool:
        """
        ## 判断默认合约的K线是否更新（实盘模式专用，属性接口）
        - 基于TQSDK的is_changing方法，检测最新K线是否有更新

        Returns:
            Optional[bool]: 
                - True：K线已更新
                - False：K线未更新
                - None：非实盘模式或未初始化
        """
        return self._is_live_trading and self._api.is_changing(
            self._btklinedataset.default_kline._dataset.tq_object.iloc[-1],
        )

    def is_key_changing(self, key: str) -> bool:
        """
        ## 判断默认合约的K线是否更新（实盘模式专用，属性接口）
        - 基于TQSDK的is_changing方法，检测最新K线是否有更新

        Returns:
            Optional[bool]: 
                - True：K线已更新
                - False：K线未更新
                - None：非实盘模式或未初始化
        """
        return self._is_live_trading and self._api.is_changing(
            self._btklinedataset.default_kline._dataset.tq_object.iloc[-1],
            key
        )

    @property
    def is_last_price_changing(self) -> bool | None:
        """
        ## 判断任意合约的最新价是否更新（实盘模式专用，属性接口）
        - 检测所有合约的最新价是否有更新，用于触发实盘交易逻辑

        Returns:
            Optional[bool]: 
                - True：至少一个合约最新价更新
                - False：所有合约最新价未更新
                - None：非实盘模式或未初始化
        """
        return self._is_live_trading and any([
            self._api.is_changing(kline.quote, 'last_price')
            for _, kline in self._btklinedataset.items()
        ])

    @property
    def position(self) -> BtPosition | Position:
        """
        ## 获取默认合约的仓位对象（属性接口）
        - 回测模式返回BtPosition，实盘模式返回TQSDK的Position，统一封装仓位操作

        Returns:
            Union[BtPosition, Position]: 默认合约的仓位对象
        """
        """第一个合约的仓位对象"""
        return self._btklinedataset.default_kline.position

    def get_results(self):
        """
        ## 获取回测结果（方法接口，与results属性功能一致）
        - 兼容旧版代码，返回所有合约的回测结果列表

        Returns:
            list[pd.DataFrame]: 回测结果列表
        """
        return self._results

    def get_btklinedataset(self):
        """
        ## 获取K线数据集合（方法接口，与klinesset属性功能一致）
        - 兼容旧版代码，返回策略管理的所有KLine对象

        Returns:
            Union[KLinesSet[str, KLine], dict[str, KLine]]: K线数据集合
        """
        return self._btklinedataset

    def get_base_dir(self):
        """
        ## 获取项目基础目录（方法接口）
        -- 返回BASE_DIR常量，用于文件路径拼接（如数据保存、报告生成）

        Returns:
            str: 项目基础目录路径
        """
        return self._base_dir

    def get_plot_datas(self):
        """
        ## 获取绘图数据（方法接口，与_plot_datas属性功能一致）
        - 返回整理后的绘图数据结构，供前端或绘图工具使用

        Returns:
            list: 绘图数据结构（包含K线、指标、配置等）
        """
        return self._plot_datas

    @property
    def agent(self):
        """
        ## 获取强化学习（RL）智能体类（属性接口）
        - 返回RL智能体的类对象（如PPO、DQN），用于初始化新的智能体实例

        Returns:
            type: RL智能体类（来自_rl_config.agent_class）
        """
        return self._rl_config.agent_class

    @property
    def env_name(self) -> str:
        """
        ## 获取强化学习（RL）环境名称（属性接口）
        - 用于标识RL环境，默认为策略类名+Env（如"MaStrategyEnv"）

        Returns:
            str: RL环境名称
        """
        """获取环境名称"""
        return self._env_args.get("env_name")

    @env_name.setter
    def env_name(self, value: str):
        """
        ## 设置强化学习（RL）环境名称（属性接口）
        - 仅接受字符串类型，用于自定义环境标识

        Args:
            value (str): 新的RL环境名称
        """
        if not isinstance(value, str):
            return
        self._env_args['env_name'] = value

    @property
    def num_envs(self) -> int:
        """
        ## 获取强化学习（RL）环境数量（属性接口）
        - 用于多环境并行训练，提升训练效率

        Returns:
            int: RL环境数量（默认1）
        """
        """获取环境数量"""
        return self._env_args.get('num_envs', 1)

    @num_envs.setter
    def num_envs(self, value: int):
        """
        ## 设置强化学习（RL）环境数量（属性接口）
        - 仅接受正整数，用于配置多环境并行训练

        Args:
            value (int): 新的RL环境数量（需>=1）
        """
        """设置环境数量"""
        if not isinstance(value, int) or value < 1:
            return
        self._env_args['num_envs'] = int(value)

    @property
    def max_step(self) -> int:
        """
        ## 获取强化学习（RL）最大步数（属性接口）
        - 表示每个RL环境的最大迭代步数（如回测数据的K线总数）

        Returns:
            int: RL最大步数（默认1000）
        """
        """获取最大步数"""
        return self._env_args.get('max_step', 1000)

    @max_step.setter
    def max_step(self, value: int):
        """
        ## 设置强化学习（RL）最大步数（属性接口）
        - 仅接受正整数，用于控制RL训练的迭代次数

        Args:
            value (int): 新的RL最大步数（需>=1）
        """
        """设置最大步数"""
        if not isinstance(value, int) or value < 1:
            return
        self._env_args['max_step'] = int(value)

    @property
    def state_dim(self) -> int:
        """
        ## 获取强化学习（RL）状态维度（属性接口）
        - 表示RL智能体输入观测值的维度（如指标数量+账户特征数量）

        Returns:
            int: RL状态维度（默认0）
        """
        """获取状态维度"""
        return self._env_args.get('state_dim', 0)

    @state_dim.setter
    def state_dim(self, value: int):
        """
        ## 设置强化学习（RL）状态维度（属性接口）
        - 仅接受正整数，需与实际观测值维度一致

        Args:
            value (int): 新的RL状态维度（需>=1）
        """
        """设置状态维度"""
        if not isinstance(value, int) or value < 1:
            return
        self._env_args['state_dim'] = int(value)

    @property
    def action_dim(self) -> int:
        """
        ## 获取强化学习（RL）动作维度（属性接口）
        - 表示RL智能体输出动作的维度（如1=单动作，3=多动作）

        Returns:
            int: RL动作维度（默认0）
        """
        """获取动作维度"""
        return self._env_args.get('action_dim', 0)

    @action_dim.setter
    def action_dim(self, value: int):
        """
        ## 设置强化学习（RL）动作维度（属性接口）
        - 仅接受正整数，需与实际动作空间一致

        Args:
            value (int): 新的RL动作维度（需>=1）
        """
        """设置动作维度"""
        if not isinstance(value, int) or value < 1:
            return
        self._env_args['action_dim'] = int(value)

    @property
    def if_discrete(self) -> bool:
        """
        ## 获取强化学习（RL）动作空间类型（属性接口）
        - 区分动作空间是离散还是连续，影响智能体选择（如DQN适用于离散，PPO适用于连续）

        Returns:
            bool: 
                - True：离散动作空间（如0=平仓，1=开多，2=开空）
                - False：连续动作空间（如动作值为仓位比例，-1~1）
        """
        """获取是否离散动作空间"""
        return self._env_args.get('if_discrete', True)

    @if_discrete.setter
    def if_discrete(self, value: bool):
        """
        ## 设置强化学习（RL）动作空间类型（属性接口）
        - 仅接受布尔值，需与智能体类型匹配

        Args:
            value (bool): 新的动作空间类型（True=离散，False=连续）
        """
        """设置是否离散动作空间"""
        if not isinstance(value, bool):
            return
        self._env_args['if_discrete'] = value

    @property
    def signal_features(self) -> np.ndarray | None:
        """
        ## 获取强化学习（RL）信号特征（属性接口）
        - 返回处理后的指标特征数组，用于RL智能体的观测值构建

        Returns:
            np.ndarray: 信号特征数组（形状：[时间步, 特征数]）

        ### 逻辑：
        - 若未初始化，调用get_signal_features生成
        - 已初始化则直接返回，避免重复计算
        """
        """获取信号特征"""
        if not self.rl:
            return self._signal_features
        if self._signal_features is None or self._is_live_trading:
            if self.__auto_process_features:
                self.get_signal_features()  # 自动处理特征（归一化、异常值、PCA等）
            else:
                self.get_raw_indicator_features()  # 仅做基础缺失值处理
        
        return self._signal_features
    
    @property
    def state(self) -> np.ndarray:
        """
        ## 获取强化学习（RL）当前状态（属性接口）
        - 返回当前时间步的信号特征数组，用于RL智能体的观测值构建

        Returns:
            np.ndarray: 当前状态数组（形状：[特征数]）
        """
        """获取当前状态"""
        if self._is_live_trading:
            # 实盘交易模式下，返回最后 window_size 个时间步的信号特征
            return self.signal_features[self._btklinedataset._max_length - self.window_size: ].flatten()
        return self.signal_features[self.btindex - self.window_size+1: self.btindex+1].flatten()
    
    @property
    def next_state(self) -> np.ndarray:
        """
        ## 获取强化学习（RL）下一个状态（属性接口）
        - 返回下一个时间步的信号特征数组，用于RL智能体的观测值构建

        Returns:
            np.ndarray: 下一个状态数组（形状：[特征数]）
        """
        """获取下一个状态"""
        if self._is_live_trading:
            # 实盘交易模式下，返回的是最后一个时间步的信号特征
            return self.signal_features[self._btklinedataset._max_length - self.window_size: ].flatten()
        return self.signal_features[self.btindex - self.window_size+2: self.btindex+2].flatten()

    @property
    def train(self) -> bool | None:
        """
        ## 获取强化学习（RL）训练状态（属性接口）
        - 标识当前是否处于RL训练模式，影响智能体行为（训练=探索，推理=利用）

        Returns:
            Optional[bool]: 
                - True：训练模式
                - False：推理模式
                - None：未初始化RL配置
        """
        """是否训练中"""
        if self._rl_config:
            return self._rl_config.train

    @train.setter
    def train(self, value: bool):
        """
        ## 设置强化学习（RL）训练状态（属性接口）
        - 切换RL智能体的训练/推理模式，如推理模式下禁用探索

        Args:
            value (bool): 新的训练状态（True=训练，False=推理）
        """
        """设置是否训练中"""
        if self._rl_config is not None:
            self._rl_config.train = bool(value)
            
    @property
    def done(self) -> bool:
        """
        ## 获取强化学习（RL）是否完成状态（属性接口）
        - 标识当前RL智能体是否已完成训练或推理，影响智能体行为（如探索=继续训练，利用=完成）

        Returns:
            bool: 
                - True：已完成训练或推理
                - False：未完成训练或推理
        """
        """是否完成"""
        return self._btindex >= self._rl_config.max_step

    @property
    def rlconfig(self) -> RlConfig | None:
        """
        ## 获取强化学习（RL）配置对象（属性接口）
        - 返回ElegantRL的Config对象，包含训练参数（如学习率、批量大小）

        Returns:
            Optional[RlConfig]: RL配置对象（未初始化则返回None）
        """
        """获取强化学习配置"""
        return self._rl_config

    @property
    def klinesset(self) -> KLinesSet[str, KLine] | dict[str, KLine]:
        """
        ## 获取K线数据集合（属性接口）
        - 返回策略管理的所有KLine对象，支持多合约场景

        Returns:
            Union[KLinesSet[str, KLine], dict[str, KLine]]: 
                - KLinesSet：增强型数据集合（支持按名称/索引访问）
                - dict：普通字典（键为合约名，值为KLine对象）
        """
        return self._btklinedataset

    @property
    def btindicatordataset(self) -> BtIndicatorDataSet[str, IndFrame | IndSeries] | dict[str, IndFrame | IndSeries]:
        """
        ## 获取所有指标对象（属性接口）
        - 返回指标数据集合中的所有指标，支持批量操作（如绘图、保存）

        Returns:
            list[BtIndType]: 指标对象列表（Line/IndSeries/IndFrame）
        """
        return self._btindicatordataset

    @property
    def klines(self) -> list[KLineType]:
        """
        ## 获取所有K线数据对象（属性接口）
        - 返回K线数据集合中的所有KLine对象，支持批量操作（如数据预处理）

        Returns:
            list[KLineType]: K线数据对象列表
        """
        return list(self._btklinedataset.values())

    def augment_shuffle_timesteps(self, n_splits_range: tuple = (2, 4)):
        """## 时序暴力打乱：破坏时间顺序，保留全局统计规律
        ### 操作：
        - 随机将时间步切割为n_splits_range指定范围内的连续片段（如(2,4)表示2-4个片段），然后随机重排这些片段的顺序。
        - 更激进：直接随机打乱时间步的顺序（完全破坏时序连续性），但保留每个时间步内的特征关联性。

        Args:
            obs: 时序数据，形状为(window_size, feature_dim)
            n_splits_range: 切割片段数量的范围，格式为(min_split, max_split)，默认(2,4)
            **kwargs: 其他扩展参数

        Returns:
            打乱后扁平化的数组，形状为(window_size * feature_dim,)
        """
        self.__check_data_enhancement()
        self._data_enhancement_funcs[0] = partial(
            self._data_enhancement_funcs[0], n_splits_range=n_splits_range)

    def augment_mask_features(self, survival_rate: float = 0.2):
        """ ## 特征毁灭性屏蔽：保留关键特征的 “幸存者偏差”
        - 操作：随机屏蔽(1-survival_rate)比例的特征（置0），强制保留至少1个特征。

        Args:
            obs: 时序数据，形状为(window_size, feature_dim)
            survival_rate: 保留特征的比例（0-1之间），默认0.2（即保留20%）
            **kwargs: 其他扩展参数

        Returns:
            屏蔽后扁平化的数组，形状为(window_size * feature_dim,)
        """
        self.__check_data_enhancement()
        self._data_enhancement_funcs[2] = partial(
            self._data_enhancement_funcs[2], survival_rate=survival_rate)

    def augment_distort_values(self, distort_ratio: float = 0.5,
                               scale_range: tuple = (-3, 3), flip_ratio: float = 0.3):
        """## 数值极端扭曲：破坏量级，保留相对关系
        ### 操作：
        - 1. 随机选择distort_ratio比例的特征，乘以10^k（k在scale_range范围内）
        - 2. 随机选择flip_ratio比例的特征进行符号反转

        Args:
            obs: 时序数据，形状为(window_size, feature_dim)
            distort_ratio: 进行量级扭曲的特征比例（0-1），默认0.5
            scale_range: 缩放因子指数范围，格式为(min_k, max_k)，默认(-3, 3)
            flip_ratio: 进行符号反转的特征比例（0-1），默认0.3
            **kwargs: 其他扩展参数

        Returns:
            扭曲后扁平化的数组，形状为(window_size * feature_dim,)
        """
        self.__check_data_enhancement()
        self._data_enhancement_funcs[3] = partial(self._data_enhancement_funcs[3], distort_ratio=distort_ratio,
                                                  scale_range=scale_range, flip_ratio=flip_ratio)

    def augment_cross_contamination(self, history_obs=None,
                                    contaminate_ratio: float = 0.3):
        """## 跨时间步特征污染：破坏时序关联性，保留特征分布
        ###操作：
        - 随机选择contaminate_ratio比例的时间步，替换为历史数据中的随机时间步特征

        Args:
            obs: 时序数据，形状为(window_size, feature_dim)
            history_obs: 历史观测列表（每个元素为扁平化数组），用于抽取污染数据，默认None
            contaminate_ratio: 被污染的时间步比例（0-1），默认0.3
            **kwargs: 其他扩展参数

        Returns:
            污染后扁平化的数组，形状为(window_size * feature_dim,)
        """
        self.__check_data_enhancement()
        self._data_enhancement_funcs[4] = partial(self._data_enhancement_funcs[4], history_obs=history_obs,
                                                  contaminate_ratio=contaminate_ratio)

    def augment_collapse_features(self, n_clusters_range: tuple = (3, 5)):
        """## 特征维度坍缩：破坏特征独立性，保留聚合信息
        ### 操作：
        - 将特征合并为n_clusters_range范围内的聚合特征，再扩展回原维度
        ### 破坏逻辑：
        - 量化特征中存在大量冗余（如不同周期的均线指标高度相关），本质规律可能隐藏在特征的聚合关系中（如 “多周期均线同时上涨”）。
        - 坍缩后仍能识别规律，说明模型学到了抽象的聚合模式。
        Args:
            obs: 时序数据，形状为(window_size, feature_dim)
            n_clusters_range: 聚合聚类数量范围，格式为(min_cluster, max_cluster)，默认(3,5)
            **kwargs: 其他扩展参数

        Returns:
            坍缩后扁平化的数组，形状为(window_size * feature_dim,)
        """
        self.__check_data_enhancement()
        self._data_enhancement_funcs[5] = partial(
            self._data_enhancement_funcs[5], n_clusters_range=n_clusters_range)

    def augment_observation(self, mask_prob: float = 0.1):
        """## 强化学习（RL）观测值数据增强：随机特征掩码
        - 随机屏蔽指定概率的特征值（置0），用于提升RL模型的鲁棒性，避免过拟合

        Args:
            obs (np.ndarray): 原始观测值数组（形状：[特征数] 或 [时间步, 特征数]）
            mask_prob (float): 特征被屏蔽的概率（范围：0~1），默认0.1（10%）

        Returns:
            np.ndarray: 掩码处理后的观测值数组（与输入形状一致）

        ### 实现逻辑：
        - 生成与输入形状相同的二进制掩码（1的概率为1-mask_prob，0的概率为mask_prob）
        - 原始观测值与掩码相乘，实现指定概率的特征随机屏蔽
        """
        self.__check_data_enhancement()
        self._data_enhancement_funcs[6] = partial(
            self._data_enhancement_funcs[6], mask_prob=mask_prob)

    def __check_data_enhancement(self):
        if not self._if_data_enhancement:
            from ..data_enhancement import data_enhancement_funcs
            self._data_enhancement_funcs = data_enhancement_funcs
            self._if_data_enhancement = True

    def _process_quant_features(
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
    ) -> np.ndarray:
        """
        ## 量化交易特征处理函数，整合归一化、异常值处理、特征变换和降维

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
        from sklearn.preprocessing import (
            StandardScaler, RobustScaler, MinMaxScaler
        )
        from sklearn.decomposition import PCA
        # 复制原始特征避免修改输入
        # X = features.copy().astype(np.float32)
        X = np.column_stack(
            tuple(ind.values for _, ind in self._btindicatordataset.items())).astype(np.float32)
        n_samples, n_features = X.shape

        # --------------------------
        # 1. 缺失值处理（时序插值）
        # --------------------------
        df = pd.DataFrame(X)
        # 线性插值（优先）+ 前后填充（确保无缺失）
        df = df.interpolate(method="linear", limit_direction="both")
        df.fillna(0.0, inplace=True)  # 仍缺失的用0填充
        X = df.values

        # --------------------------
        # 2. 异常值处理
        # --------------------------
        if handle_outliers == "clip":
            # 按特征维度截断极端值（四分位法）
            for col in range(n_features):
                feature = X[:, col]
                q1, q3 = np.percentile(feature, [25, 75])
                iqr = q3 - q1
                lower = q1 - 1.5 * iqr  # 下边界
                upper = q3 + 1.5 * iqr  # 上边界
                X[:, col] = np.clip(feature, lower, upper)
        elif handle_outliers == "mark":
            # 新增异常值标记特征（0=正常，1=异常）
            outlier_masks = []
            for col in range(n_features):
                feature = X[:, col]
                q1, q3 = np.percentile(feature, [25, 75])
                iqr = q3 - q1
                is_outlier = (feature < (q1 - 1.5 * iqr)
                              ) | (feature > (q3 + 1.5 * iqr))
                outlier_masks.append(
                    is_outlier.astype(np.float32).reshape(-1, 1))
            # 拼接原始特征和异常值标记
            X = np.concatenate([X] + outlier_masks, axis=1)
            n_features = X.shape[1]  # 更新特征数

        # --------------------------
        # 3. 特征变换（压缩长尾分布）
        # --------------------------
        if use_log_transform:
            for col in range(n_features):
                feature = X[:, col]
                # 仅对非负特征应用对数变换（避免负数问题）
                if np.min(feature) >= 0:
                    X[:, col] = np.log1p(feature)  # log(1 + x)，避免log(0)

        # --------------------------
        # 4. 特征选择（基于与目标收益率的相关性）
        # --------------------------
        if target_returns is not None:
            # 计算特征与目标收益率的相关性
            corr = np.array([np.corrcoef(X[:, col], target_returns)[
                            0, 1] for col in range(n_features)])
            corr_abs = np.abs(corr)
            # 保留相关性前80%的特征（或至少保留10个特征）
            threshold = np.percentile(corr_abs, 20) if n_features > 10 else 0
            keep_cols = corr_abs >= threshold
            X = X[:, keep_cols]
            n_features = X.shape[1]
            if n_features == 0:  # 避免全部特征被过滤
                X = X.copy()  # 回退到原始特征

        # --------------------------
        # 5. 归一化（核心参数控制）
        # --------------------------
        if normalize_method == "standard":
            scaler = StandardScaler()
            X = scaler.fit_transform(X)
        elif normalize_method == "robust":
            scaler = RobustScaler(quantile_range=(25.0, 75.0))
            X = scaler.fit_transform(X)
        elif normalize_method == "minmax":
            scaler = MinMaxScaler(feature_range=feature_range)
            X = scaler.fit_transform(X)
        elif normalize_method == "rolling":
            # 滚动窗口内标准化（避免未来数据泄露）
            X_rolling = np.zeros_like(X)
            for i in range(n_samples):
                # 窗口范围：[i-rolling_window, i)（不包含当前i，避免未来数据）
                start = max(0, i - rolling_window)
                window = X[start:i]
                if len(window) < 10:  # 窗口样本不足时不标准化
                    X_rolling[i] = X[i]
                else:
                    mean = window.mean(axis=0)
                    std = window.std(axis=0) + 1e-6  # 避免除零
                    X_rolling[i] = (X[i] - mean) / std
            X = X_rolling

        # --------------------------
        # 6. PCA降维（减少冗余）
        # --------------------------
        if pca_n_components < 1.0 or (pca_n_components > 1 and pca_n_components < n_features):
            pca = PCA(n_components=pca_n_components)
            X = pca.fit_transform(X)
        self._signal_features = X
        return X

    def get_raw_indicator_features(self) -> np.ndarray:
        """
        ## 使用指标的原始数据作为特征，仅做基础的缺失值处理

        - 从 _btindicatordataset 中提取所有指标的原始值，不做归一化、
          异常值处理、PCA 等高级变换，仅处理 NaN 和 Inf 值。
        - 适用于需要自行处理特征保真度的场景。

        Returns:
            np.ndarray: 处理后的特征数组，形状为(时间步, 特征数)
        """
        # 从指标数据集中提取所有指标的原始值
        X = np.column_stack(
            tuple(ind.values for _, ind in self._btindicatordataset.items())
        ).astype(np.float64)

        # 处理无穷值
        X = np.where(np.isfinite(X), X, np.nan)

        # 缺失值处理：线性插值 + 前后填充
        for col in range(X.shape[1]):
            col_data = X[:, col]
            mask = np.isnan(col_data)
            if not mask.any():
                continue
            # 前向填充
            for i in range(1, len(col_data)):
                if np.isnan(col_data[i]):
                    col_data[i] = col_data[i - 1]
            # 后向填充（处理开头的 NaN）
            for i in range(len(col_data) - 2, -1, -1):
                if np.isnan(col_data[i]):
                    col_data[i] = col_data[i + 1]
            # 仍为 NaN 的补 0
            col_data[np.isnan(col_data)] = 0.0
            X[:, col] = col_data

        self._signal_features = X
        return X

    @staticmethod
    def get_max_missing_count(*args: tuple[pd.Series, np.ndarray]) -> int:
        """## 参数必须为np.ndarray或pandas.Series，返回输入中缺失值数量的最大值"""
        if not args:
            return 0
        result = [len(arg[pd.isnull(arg)])
                  for arg in args if isinstance(arg, (pd.Series, np.ndarray))]
        if len(result) == 1:
            return result[0]
        return max(result)

    def kline_from_path(self, path: str, **kwargs) -> KLine:
        return self.get_kline(path, **kwargs)

    def kline_from_dataframe(self, data: pd.DataFrame, **kwargs) -> KLine:
        return self.get_kline(data, **kwargs)

    def kline_from_tqsdk(self, symbol: str,
                         duration_seconds: int,
                         data_length: int = 300,
                         chart_id: str | None = None,
                         adj_type: str | None = None,
                         user_name: str = "",
                         password: str = "") -> KLine:
        kwargs = dict(chart_id=chart_id, adj_type=adj_type,
                      user_name=user_name, password=password)
        return self.get_kline(symbol=symbol, duration_seconds=duration_seconds, data_length=data_length, **kwargs)

    def kline_from_pytdx(self, symbol: str, cycle: int, length=800, **kwargs) -> KLine:
        kwargs.update(dict(data_source="pytdx"))
        return self.get_kline(symbol, cycle, length, **kwargs)

    def kline_from_baostock(self,
                            code: str,
                            fields: str | None = None,
                            start_date: Any | None = None,
                            end_date: Any | None = None,
                            frequency: Literal["5", "15",
                                               "30", "60", "d", "w", "m"] = "5",
                            adjustflag: Literal["1", "2", "3"] = "1",
                            **kwargs,
                            ) -> KLine:
        kwargs.update(dict(fields=fields, start_date=start_date,
                      end_date=end_date, adjustflag=adjustflag, data_source="baostock"))
        return self.get_kline(code, frequency, **kwargs)

    def kline_stock_from_akshare(self,
                                 symbol: str = "000001",
                                 period: Literal['daily', 'weekly',
                                                 'monthly'] = "daily",
                                 start_date: str = "19700101",
                                 end_date: str = "20500101",
                                 adjust: Literal["qfq", "hfq", ""] = "",
                                 timeout: float = None,
                                 **kwargs) -> KLine:
        kwargs.update(dict(start_date=start_date, end_date=end_date,
                      adjust=adjust, timeout=timeout, data_source="akshare"))
        # 剥离交易所前缀，如 "sh.600000" → "600000"、"sz.000001" → "000001"
        if '.' in symbol:
            symbol = symbol.split('.', 1)[1]
        return self.get_kline(symbol, period, **kwargs)

    def kline_futures_from_akshare(self,
                                   symbol: str = "热卷主连",
                                   period: Literal["1", "5", "15", "30", "60",
                                                   "daily", "weekly", "monthly"] = "1",
                                   start_date: str = "19900101",
                                   end_date: str = "20500101",
                                   **kwargs) -> KLine:
        kwargs.update(dict(start_date=start_date,
                      end_date=end_date, data_source="akshare"))
        return self.get_kline(symbol, period, **kwargs)

    def kline_random(self,
                     symbol: str = "symbol",
                     duration_seconds: int = 60,
                     data_length: int | None = 1000,
                     price_tick: float = 1e-2,
                     volume_multiple: float = 1.,
                     base_price: float = 100.0,
                     base_volume: float = 1000.0,
                     volatility: float = 0.02) -> KLine:
        """
        ### 生成随机K线数据

        Args:
            symbol: 交易品种符号
            duration_seconds: K线周期时长（秒），60表示1分钟K线
            data_length: 生成的数据条数
            price_tick: 最小变动单位
            volume_multipl: 合约乘数
            base_price: 基础价格水平
            base_volume: 基础成交量水平
            volatility: 基础波动率（小时级别）

        Returns:
            KLine: 包含随机K线数据的对象
        """
        # 设置随机种子以保证结果可重现
        random = np.random
        random.seed(42)

        # 根据周期调整波动率（周期越长波动越大）
        # 使用平方根规则：波动率与时间的平方根成正比
        time_factor = np.sqrt(duration_seconds / 3600)
        adjusted_volatility = volatility * time_factor

        # 计算时间间隔
        time_delta = timedelta(seconds=duration_seconds)

        # 生成时间序列（从当前时间往前推）
        end_time = datetime.now()
        start_time = end_time - time_delta * data_length

        kline_data = []
        current_time = start_time

        # 初始化价格
        prev_close = base_price

        # 市场状态变量
        market_state = "oscillation"  # 初始状态为震荡
        state_duration = random.randint(15, 30)  # 初始震荡持续时间
        trend_direction = 0  # 趋势方向
        trend_strength = 0  # 趋势强度
        oscillation_center = base_price  # 震荡中心价格

        # 价格记忆，用于计算支撑阻力
        recent_highs = []
        recent_lows = []

        for i in range(data_length):
            # 1. 检查是否应该切换市场状态
            state_duration -= 1
            if state_duration <= 0:
                if market_state == "oscillation":
                    # 震荡结束后可能转为趋势或继续震荡
                    if random.random() < 0.3:  # 30%概率转为趋势
                        market_state = "trend"
                        trend_direction = random.choice([-1, 1])
                        trend_strength = random.uniform(0.8, 1.5)  # 降低趋势强度
                        state_duration = random.randint(5, 12)  # 缩短趋势持续时间
                    else:
                        # 继续震荡
                        market_state = "oscillation"
                        state_duration = random.randint(20, 40)  # 延长震荡持续时间
                        # 更新震荡中心为当前价格
                        oscillation_center = prev_close
                else:  # 趋势状态
                    # 趋势结束后转为震荡
                    market_state = "oscillation"
                    state_duration = random.randint(25, 50)  # 延长震荡持续时间
                    oscillation_center = prev_close

            # 2. 决定是否生成大阳线/大阴线 (1%的概率，大幅降低)
            big_move = False
            if random.random() < 0.01 and market_state == "trend":
                big_move = True
                trend_strength = random.uniform(1.2, 1.8)  # 降低大阳线强度

            # 3. 根据市场状态生成价格变动
            if market_state == "trend":
                # 趋势状态：方向与趋势一致
                direction = trend_direction
                current_volatility = adjusted_volatility * trend_strength

                # 趋势中偶尔会有回调（20%概率）
                if random.random() < 0.2:
                    direction = -direction * 0.5  # 反向但强度减半
                    current_volatility *= 0.8
            else:
                # 震荡状态：更随机的方向
                direction = random.choice([-1, 1])
                current_volatility = adjusted_volatility * \
                    random.uniform(0.7, 1.2)  # 波动更随机

                # 震荡中价格有回归中心的倾向
                distance_from_center = (
                    prev_close - oscillation_center) / oscillation_center
                if abs(distance_from_center) > 0.02:  # 偏离中心超过2%
                    # 增加回归中心的倾向
                    if distance_from_center > 0:
                        direction = -1  # 价格高于中心，倾向于下跌
                    else:
                        direction = 1  # 价格低于中心，倾向于上涨
                    # 回归力度与偏离程度成正比
                    current_volatility *= (1 + abs(distance_from_center) * 3)

            # 4. 开盘价：基于前收盘价，震荡中跳空更小
            if market_state == "oscillation":
                gap_factor = random.uniform(-0.001, 0.001)  # 震荡中跳空更小
            else:
                gap_factor = random.uniform(-0.002, 0.002)

            open_price = prev_close * (1 + gap_factor)

            # 5. 收盘价
            if big_move:
                price_change = current_volatility * \
                    direction * random.uniform(1.2, 1.8)
            elif market_state == "trend":
                price_change = current_volatility * \
                    direction * random.uniform(0.5, 1.2)
            else:
                # 震荡中价格变动更小，更随机
                price_change = current_volatility * \
                    direction * random.uniform(0.3, 0.9)

            close_price = open_price * (1 + price_change)

            # 6. 生成合理的最高价和最低价
            body_size = abs(close_price - open_price)

            # 影线长度：震荡中影线相对较长
            if market_state == "oscillation":
                upper_shadow_ratio = random.uniform(0.2, 0.8)  # 震荡中影线更长
                lower_shadow_ratio = random.uniform(0.2, 0.8)
            else:
                upper_shadow_ratio = random.uniform(0.2, 1.)
                lower_shadow_ratio = random.uniform(0.2, 1.)

            upper_shadow = body_size * upper_shadow_ratio
            lower_shadow = body_size * lower_shadow_ratio

            # 计算最高价和最低价
            if close_price > open_price:  # 阳线
                high_price = close_price + upper_shadow
                low_price = open_price - lower_shadow
            else:  # 阴线
                high_price = open_price + upper_shadow
                low_price = close_price - lower_shadow

            # 震荡行情中允许K线范围大部分重叠，放宽限制
            if i > 0:
                prev_high = kline_data[i-1]['high']
                prev_low = kline_data[i-1]['low']

                # 只有在趋势中且重叠过多时才调整
                if market_state == "trend":
                    # 如果价格范围与前一根K线重叠过多，调整高低点
                    # 计算重叠比例
                    overlap_min = max(low_price, prev_low)
                    overlap_max = min(high_price, prev_high)

                    # 检查是否有重叠
                    if overlap_max > overlap_min:
                        overlap_length = overlap_max - overlap_min
                        union_length = max(
                            high_price, prev_high) - min(low_price, prev_low)
                        overlap_ratio = overlap_length / union_length if union_length > 0 else 1
                    else:
                        overlap_ratio = 0  # 没有重叠

                    # 根据重叠比例区间调整
                    if overlap_ratio >= 0.9:  # [0.9, 1] 区间
                        # 高度重叠，需要较大调整
                        adjustment_factor = random.uniform(
                            0.6, 0.8)  # 调整幅度因子40%-60%
                    elif overlap_ratio >= 0.8:  # [0.8, 0.9) 区间
                        # 较高重叠，需要中等调整
                        adjustment_factor = random.uniform(
                            0.35, 0.65)  # 调整幅度因子30%-45%
                    elif overlap_ratio >= 0.7:  # [0.7, 0.8) 区间
                        # 中等重叠，需要较小调整
                        adjustment_factor = random.uniform(
                            0.3, 0.55)  # 调整幅度因子20%-35%
                    elif overlap_ratio >= 0.6:  # [0.6, 0.7) 区间
                        # 较低重叠，需要轻微调整
                        adjustment_factor = random.uniform(
                            0.25, 0.5)  # 调整幅度因子10%-25%
                    elif overlap_ratio >= 0.5:  # [0.5, 0.6) 区间
                        # 轻微重叠，需要很小调整
                        adjustment_factor = random.uniform(
                            0.2, 0.45)  # 调整幅度因子5%-15%
                    elif overlap_ratio >= 0.4:  # [0.4, 0.5) 区间
                        # 轻微重叠，需要很小调整
                        adjustment_factor = random.uniform(
                            0.15, 0.4)  # 调整幅度因子5%-15%
                    elif overlap_ratio >= 0.3:  # [0.4, 0.5) 区间
                        # 轻微重叠，需要很小调整
                        adjustment_factor = random.uniform(
                            0.1, 0.35)  # 调整幅度因子5%-15%
                    else:
                        # 重叠小于50%，不调整
                        adjustment_factor = 0

                    # 如果需要调整
                    if adjustment_factor > 0:
                        # 基于body_size计算调整幅度
                        adjustment = body_size * adjustment_factor

                        if direction > 0:  # 上涨趋势，提高高点
                            high_price += adjustment
                            # 确保高点高于前高点
                            high_price = max(high_price, prev_high * 1.001)
                        else:  # 下跌趋势，降低低点
                            low_price -= adjustment
                            # 确保低点低于前低点
                            low_price = min(low_price, prev_low * 0.999)

                        # 确保调整后的低点不为负
                        low_price = max(low_price, base_price * 0.01)

                        # 确保高低价关系正确
                        high_price = max(high_price, max(
                            open_price, close_price))
                        low_price = min(low_price, min(
                            open_price, close_price))

            # 记录近期高低点
            if len(recent_highs) < 10:
                recent_highs.append(high_price)
            else:
                recent_highs.pop(0)
                recent_highs.append(high_price)

            if len(recent_lows) < 10:
                recent_lows.append(low_price)
            else:
                recent_lows.pop(0)
                recent_lows.append(low_price)

            # 7. 成交量：震荡中成交量通常较小
            volatility_factor = body_size / open_price
            volume_factor = 1 + volatility_factor * 15  # 降低成交量对波动的敏感度

            # 不同市场状态的成交量
            if market_state == "trend":
                volume_factor *= 1.3  # 趋势中成交量放大
            else:
                volume_factor *= 0.8  # 震荡中成交量缩小

            # 大阳线/大阴线成交量放大
            if big_move:
                volume_factor *= 1.5

            # 添加随机成交量变化
            random_volume_factor = random.uniform(0.7, 1.3)

            volume = int(base_volume * volume_factor * random_volume_factor)

            # 8. 创建K线数据点
            kline_point = {
                'datetime': current_time,
                'open': round(open_price, 4),
                'high': round(high_price, 4),
                'low': round(low_price, 4),
                'close': round(close_price, 4),
                'volume': volume
            }

            kline_data.append(kline_point)

            # 9. 更新时间并设置下一根K线的起始价格
            current_time += time_delta
            prev_close = close_price

            # 10. 如果价格接近近期高低点，可能会有阻力/支撑效应
            if len(recent_highs) >= 5 and len(recent_lows) >= 5:
                avg_high = np.mean(recent_highs)
                avg_low = np.mean(recent_lows)

                # 如果价格接近近期高点，可能遇到阻力
                if prev_close > avg_high * 0.98 and market_state == "trend" and trend_direction > 0:
                    # 阻力效应：可能减弱上涨趋势
                    if random.random() < 0.4:  # 40%概率减弱趋势
                        trend_strength *= 0.8

                # 如果价格接近近期低点，可能遇到支撑
                if prev_close < avg_low * 1.02 and market_state == "trend" and trend_direction < 0:
                    # 支撑效应：可能减弱下跌趋势
                    if random.random() < 0.4:  # 40%概率减弱趋势
                        trend_strength *= 0.8

        # 转换为DataFrame并返回指定格式
        df = pd.DataFrame(kline_data)
        df['symbol'] = symbol
        df['duration'] = duration_seconds
        df['price_tick'] = price_tick
        df['volume_multiple'] = volume_multiple
        return self.kline_from_dataframe(df)
