# Utils

Qlib service 通用工具与适配器。

## StrategyAdapter

- 负责策略配置的路径解析与参数清理。
- 兼容处理不被 Qlib 策略接受的参数（如移除 `buffer_margin`）。
- 默认补充增强指数优化器参数（`lamb/delta/b_dev`）以提高数值稳定性。

## SimpleSignal

- 负责加载 `pred.pkl` 或回退到 Qlib 特征（如 `$close`）。
- 回退模式会按日期切片，输出以股票代码为索引的序列，避免多级索引导致的优化失败。
- 读取 `pred.pkl` 时会自动检测并对齐股票代码大小写（如 `SH600000` -> `sh600000`），避免因代码风格不一致导致策略无法成交。

## 近期更新
- 交易流水数量还原稳定性修复（2026-04-22）：
  - `recording_strategy.py` 的展示数量写入新增 A 股整手容差纠偏（容差 `<=2` 股）；
  - 当复权因子跨日微漂移导致数量落在 `2701/2702` 等近整手值时，自动回吸到最近 `100` 股整手；
  - 目标是保持成交数量展示与 A 股最小交易单位一致，避免回测流水出现不合理“碎股”。
- 末日估值行情回看兜底（2026-04-12）：
  - `cn_exchange.py` 新增“最近有效行情回看”逻辑；
  - 当回测末端或数据断档日出现 `$close/$factor/open/close` 为 `0/NaN` 时，会在最近 `10` 个自然日内向前回查最近一笔有效值后再用于成交/估值；
  - 环境变量 `QLIB_QUOTE_FALLBACK_LOOKBACK_DAYS` 可调整回看天数，默认 `10`；
  - 目标是避免数据更新不完整时，持仓在结束日被按 `0` 估值导致净值曲线尾部“断崖式跳空”。
- 配置适配与 WebSocket 日志收口（2026-04-10）：
  - `strategy_adapter.py` 的参数补全/清洗日志已统一为结构化事件格式；
  - `websocket/connection_manager.py` 的连接、断开、广播与清理日志已统一为结构化事件格式；
  - 这些底层日志现在与回测、分析和任务链路共用同一排障口径。
- 运行时日志继续收口（2026-04-10）：
  - `backtest_service_runtime.py` 在信号加载、资源解析与过期清理等关键点继续使用结构化日志；
  - `trade_stats_service.py` 也已统一为结构化日志实例，便于与回测运行日志对齐。
- 交易撮合与保证金日志收口（2026-04-10）：
  - `cn_exchange.py` 与 `margin_position.py` 的关键日志已统一为结构化事件格式；
  - 交易限制、Redis 交易记录初始化、参与率异常与计息扣除现在统一输出 `event=... key=value`。
- 核心工具日志收口（2026-04-10）：
  - `qlib_utils.py`、`simple_signal.py`、`cn_exchange.py`、`margin_position.py`、`recording_strategy.py`、`extended_strategies.py` 的关键日志持续统一为结构化事件格式；
  - 这样信号解析、后端兼容、缓存命中与交易撮合相关问题都能在同一日志 schema 下检索。
- 策略与工具层日志继续统一（2026-04-10）：
  - 继续收口 `recording_strategy.py` 与 `extended_strategies.py` 的策略输出日志，使其与上层任务/接口日志共享同一结构化 schema；
  - 这样前端日志流、worker 控制台与分析接口的排障口径保持一致。
- 结构化日志统一（2026-04-10）：
  - 新增 `structured_logger.py`，用于将关键运行日志统一输出为 `event=... key=value` 形式；
  - `log_pusher.py` 的初始化/错误日志也改为结构化格式，便于与任务级日志对齐；
  - 任务级日志现在以 Redis handler 为主，避免同一链路在 Redis 中出现双写。
- 全量截面 Alpha 策略（2026-03-29）：
  - `extended_strategies.py` 新增 `RedisFullAlphaStrategy`；
  - 每次调仓按预测分全量重构 TopK，跌出 TopK 的标的不受 `n_drop` 限制全部卖出；
  - 买入侧遇到涨停/停牌会自动顺延到下一只候选标的补位，尽量维持目标持仓数。
- TopK 默认调仓比例统一（2026-03-29）：
  - `strategy_adapter._adapt_topk_dropout` 在 `n_drop` 缺失时改为按 `topk * 20%` 自动推导（四舍五入且至少为 1），不再固定回退为 5。
- 保证金多空回测修复（2026-03-15）：
  - `margin_position.py` 兼容 qlib `0.9.x` 的 `Order` 导入路径，避免 `qlib.backtest.order` 缺失导致回测/测试直接导入失败；
  - `MarginPosition.calculate_stock_value` 改为仅遍历真实持仓项，忽略 `now_account_value` 等元字段，修复空头估值时的运行期崩溃；
  - `MarginAccount` 新增成交前持仓快照，用于正确统计“全平空/跨零换向”场景下的实现收益，并继续将手续费留在 `cost` 口径单独统计。
- 原生多空 TopK 策略（2026-03-15）：
  - `extended_strategies.py` 新增 `RedisLongShortTopkStrategy`，原生支持“做多最高分 TopK + 做空最低分 TopK”。
  - 支持 `short_topk/long_exposure/short_exposure/max_weight/rebalance_days` 参数，供模板和回测引擎直接调用。
- 策略参数清洗修复（2026-03-12）：
  - `recording_strategy._OUR_KWARGS` 新增 `pool_file_local`；
  - 修复智能策略生成场景下 `BaseStrategy.__init__() got an unexpected keyword argument 'pool_file_local'`。
- SimpleSignal 股票池解析修复（2026-03-11）：
  - 修复 `<PRED>` 信号路径下 `universe` 为文件路径（如 `instruments/csi1000.txt`）时未生效的问题；
  - 新增股票池文件解析与相对路径解析（`cwd`、项目根目录、`db/qlib_data`、`/data/qlib_data`），并统一复用到信号预过滤、代码大小写对齐与特征信号回退路径；
  - 当股票池解析失败时不再静默放大全市场结果，会记录告警并返回空池信号，避免“不同股票池结果完全一致”的隐性误判。
- 动态仓位日期索引修复（2026-03-08）：
  - `DynamicRiskMixin._get_trade_date` 修复未定义变量 `idx`，回退分支统一按 `trade_step` 安全取值；
  - 避免异常被吞后长期返回 `None`，导致动态仓位逻辑退化为默认仓位。
- 策略重置签名与调仓周期修复（2026-03-08）：
  - `DynamicRiskMixin` 新增 `reset(*args, **kwargs)` 兼容层，支持 qlib 在回测循环中调用 `reset(level_infra=...)`，避免 `unexpected keyword argument 'level_infra'`。
  - `RedisRecordingStrategy` / `RedisWeightStrategy` / `RedisTopkStrategy` 的调仓判定统一走 `_should_rebalance`，在不同 qlib 版本下稳定读取交易步长；
  - 当运行时无法读取 `trade_step` 时，回退到本地步长计数器，确保 `rebalance_days` 不会失效。
- SimpleSignal 回测性能优化（2026-03-06）：
  - 逐日回测场景增加按交易日缓存，避免每个交易日重复全量切片 `pred.pkl`；
  - 时间范围切片优先使用 `MultiIndex` 范围索引，减少布尔掩码扫描；
  - 加载 `pred.pkl` 后按 `universe` 预过滤，降低非目标股票池带来的回测开销。
- SimpleSignal 代码映射增强（2026-02-28）：
  - 在原有大小写对齐之外，新增 `000001` / `600000.SH` 等格式向 Qlib 标准代码（如 `SZ000001` / `SH600000`）的自动映射；
  - 解决 `pred.pkl` 信号存在但与 `universe` 代码格式不一致时的 0 成交问题。
- 交易流水价格口径修正（`recording_strategy`）：
  - 对外字段 `price` 改为**非复权价**（由 `adj_price / factor` 转换）；
  - 对外字段 `quantity` 改为**整数股数**（由复权数量换算后取整）；
  - 新增追踪字段 `adj_price`、`adj_quantity`、`factor`，用于和 Qlib 内部复权口径对账。
- 交易流水字段增强：`recording_strategy` 写入 Redis 的每笔成交新增
  - `totalAmount`（成交金额）
  - `cash_after`（成交后现金）
  - `position_value_after`（成交后持仓市值）
  - `equity_after`（成交后总资产）
  并保留 `balance` 兼容旧前端（映射为 `equity_after`）。
- 修复 `extended_strategies` 中 mixin 初始化参数传递：
  - `init_redis/init_dynamic_risk` 统一使用 `kwargs` 字典入参（而非 `**kwargs` 展开）；
  - 解决默认 `standard_topk` 回测时 `RedisLoggerMixin.init_redis() got an unexpected keyword argument 'signal'`。
- SimpleWeightStrategy 兼容 Qlib 变体调用签名（位置参数/关键字参数），避免回测报错。
- SimpleWeightStrategy 支持 `topk`、`min_score`、`max_weight` 参数，用于遗传算法优化。
- RedisRecordingStrategy/RedisWeightStrategy 支持动态仓位（按市场状态动态调整 `risk_degree`）。
- RedisWeightStrategy 回测兼容修复（2026-03-09）：
  - `reset` 方法签名改为 `*args, **kwargs` 并透传父类，兼容 qlib 新版本 `reset(level_infra=...)` 调用，避免 `unexpected keyword argument 'level_infra'`。
  - `generate_target_weight_position` 增加 `t_start/t_end` 安全读取，避免未定义变量导致的运行期异常。
- 自定义策略 `reset` 签名兼容性加固（2026-03-09）：
  - `RedisTopkStrategy` / `RedisRecordingStrategy` / `RedisWeightStrategy` 的 `reset` 统一采用 `*args, **kwargs`；
  - 对 `level_infra/common_infra/trade_exchange` 执行兼容回退：先原样调用，失败后剔除不兼容参数重试，最终回退到无参 `reset()`；
  - 清理 `RedisRecordingStrategy` 中重复的旧版 `reset(self, common_infra=None)` 定义，避免维护混乱与行为歧义。
