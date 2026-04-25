# Frontend Services

前端服务层，负责与后端微服务通信。

## 目录结构

- `api-client.ts`: 统一的 Axios API 客户端，含拦截器、重试、错误处理；支持通过请求配置标记跳过 5xx 重试，供 QuantBot 在 openclaw 会话服务不可用时静默降级。
- `config.ts`: API 端点配置。
- `userService.ts`: 用户服务与通知中心 API 适配。
- `marketService.ts`: 市场数据服务。
- `portfolioService.ts`: [New] 投资组合与模拟交易服务。
- `tradingService.ts`: [New] 交易订单服务。
- `strategyService.ts`: 策略列表、启停与回测相关服务。
- `backtestService.ts`: 回测任务提交、状态轮询、增量日志拉取、任务停止、报告导出与参数优化（Qlib）。
- `refreshOrchestrator.ts`: 统一模块刷新协调器（按模块节流、并发去重、全局触发）。
- `websocketService.ts`: 全局 WebSocket 连接管理（行情/交易/通知订阅、心跳、断线重连）。

## 新增服务说明

- `modelTrainingService` 用户态训练闭环（2026-04-04）：
  - 新增用户态接口适配：`getFeatureCatalog`、`runTraining`、`getTrainingRun`，分别对接 `/api/v1/models/feature-catalog|run-training|training-runs/{run_id}`；
  - 请求头统一透传 `Authorization` 与 `X-Tenant-Id`，并复用 `authService.handle401Error` 处理鉴权失效；
  - 训练入参改为页面全量参数透传（`target/label/context/early_stopping/feature_categories` 等），避免 admin 语义与用户态接口混用。
  - 新增用户模型管理能力：`listUserModels/getDefaultModel/setDefaultModel/getUserModel/archiveUserModel`；
  - 新增 SHAP 归因读取能力：`getModelShapSummary`，对接 `/api/v1/models/{model_id}/shap-summary`，返回 `feature/mean_abs_shap/mean_shap/positive_ratio` 列表用于归因分析页展示；
  - 新增策略绑定能力：`getStrategyBinding/setStrategyBinding/deleteStrategyBinding`；
  - 训练结果可直接消费后端 `result.model_registration`（`model_id/status/error`）用于前端“同步状态 + 设默认”闭环。
  - 模型推理中心新增交易日历能力：
    - `checkTradingDay/nextTradingDay/prevTradingDay` 对接 `/api/v1/market-calendar/*`；
    - `resolveInferenceDateByCalendar` 用于将非交易日自动校正到最近上一交易日；
    - `calcTargetDateByCalendar` 用于按真实交易日历计算 `T+N` 目标日期。

- `componentCodeGenerator` 性能报告模板文案口径更新（2026-03-09）：
  - 输出文案中的 `胜率` 调整为 `投资胜率`；
  - 移除模板报告里的 `Beta`、`信息比率` 展示项，避免与回测中心当前展示口径不一致。
- `communityService` 社区互动接口修复（2026-03-09）：
  - 写操作（点赞/收藏/评论/发帖/编辑/删除）统一增加登录态前置校验，未登录时直接抛出可读错误，避免“点击无响应”；
  - 点赞与收藏按后端语义分离为 `POST`（置为已点赞/已收藏）与 `DELETE`（取消），减少状态反转和重复点击导致的不一致。
  - 新增作者关注接口：`getAuthorFollowStatus`、`followAuthor`、`unfollowAuthor`，用于社区详情页“关注作者”真实状态读写。
- `backtestService` 轮询鉴权修复（2026-03-09）：
  - `pollStatus` 在 `catch` 阶段终态判定改为优先读取 `error.response.status`（401/403/404 直接终止轮询），避免仅依赖错误字符串导致的 401 重试刷屏。
- `backtestService` 快速导出数量口径修复（2026-04-22）：
  - `buildQuickTradeRows` 改为显式 `price/quantity` 优先，避免已有展示口径被 `adj_* * factor` 再次覆盖；
  - A 股成交量新增整手容差纠偏（容差 `<=2` 股），减少复权因子日间微漂移导致的近整手抖动。
- `backtestService` 完成态结果补全（2026-04-08）：
  - `pollStatus` 在 `completed` 状态下改为调用 `getResult(backtestId, false)`，确保回测结果页拿到完整交易明细而不是摘要数据；
  - 这样 `QlibResultDisplay` 的“调仓交易日”统计、交易详情弹窗和 CSV 导出可以复用同一份完整交易行数据。
- `backtestService` 任务停止收敛（2026-04-10）：
  - 新增通用 `stopTask(taskId)`，对接后端 `/api/v1/qlib/task/{task_id}/stop`；
  - AI-IDE 模块型策略会在提交 Qlib 异步回测后自动复用 `pollStatus`，停止按钮则走同一任务停止入口。
- `backtestService` 回测日志回填（2026-04-10）：
  - `pollStatus` 现在会同步轮询 `/api/v1/qlib/logs/{backtest_id}` 并通过 `onLog` 回调输出增量日志；
  - AI-IDE 模块型策略的运行结果区不再只刷 `running` 状态，而是直接展示 Qlib worker 的原始日志流。
- `backtestService` 导出兜底补明细（2026-04-08）：
  - `exportCSV` / `exportJSON` 的本地兜底路径改为完整结果模式；
  - 当完整结果仍缺少交易明细时，导出逻辑会继续调用 `/api/v1/qlib/results/{backtest_id}/trades` 补取后再生成 CSV。
- `backtestService` 策略对比摘要化（2026-04-08）：
  - `compareBacktests` 对接后端摘要结果，避免回测对比一次性加载完整交易明细；
  - 前端对比页兼容 `backtest1/backtest2` 与旧字段 `result_1/result_2`。
- `userService` 通知口径收敛（2026-03-10）：
  - `getNotifications` 统一消费 `{ code, data: { items, total, unread_count, has_more } }`；
  - `UserNotification` 字段口径统一为 `title/content/type/level/is_read/action_url/created_at/read_at/expires_at`；
  - 请求失败时返回 `success:false` + `degraded:true`，由通知中心保留旧数据并降级展示。
  - 新增 `clearNotifications(days?)`，对接后端 `POST /api/v1/notifications/clear`，支持按时间窗口批量清除。
- `marketDataService` 股票接口路径与 CORS 兼容修复（2026-03-20）：
  - 股票搜索/详情/热门/快速搜索/筛选/行业列表接口统一从 `/api/stocks/*` 切换为 `/api/v1/stocks/*`，与网关收敛路由保持一致；
  - 解决本地开发源 `http://127.0.0.1:3000` 直连 `https://api.quantmind.cloud` 时，旧路径触发预检但无有效 `Access-Control-Allow-Origin` 导致的 `ERR_NETWORK`。
  - `getStockDetail` 已切换为标准详情路径 `/api/v1/stocks/{symbol}`，并在详情接口异常时自动回退到 `/api/v1/stocks/search` 做名称匹配，降低持仓页“股票名称显示为代码”的概率。

- 旧本地通知中心链路已移除（2026-03-10）：
  - 删除 `services/alert/*` 本地通知与价格/指标告警主链路；
  - 仪表盘通知统一以 `userService + useNotifications + websocketService` 为唯一数据源。

### Real Trading Service

负责实盘控制台的控制面与账户查询。

- **主要方法**: `preflight`, `start`, `stop`, `getStatus`, `getLogs`, `getAccount`, `getSimulationAccount`, `getSimulationSettings`, `updateSimulationSettings`, `getSimulationDailySnapshots`
- **手动执行调试**: 已新增 `createManualExecution/listManualExecutions/getManualExecution/getManualExecutionLogs`，对应实盘页手动执行 Drawer 的任务创建、任务列表与任务级日志拉取。
- **状态兼容**: `RealTradingStatus.status` 已扩展支持 `starting` / `error`，与后端状态口径对齐。
- **错误可读性**: 新增 `getFriendlyError`，对网络失败、网关 5xx、鉴权失败等映射为用户可读文案；当交易服务不可达时，提示会优先指向网关/交易容器连通性，而不是直接把问题归因到前端配置。
- **配置告警**: 新增 `getConfigWarning`，当 `VITE_REAL_TRADING_API_URL` 设置了直连地址且未指向 `/api/v1/real-trading` 时给出前端提示；若无直连需求，可留空并使用网关默认地址。
- **控制面回退**: `preflight/start/stop/status/logs/account/manual-executions` 现在支持“网关优先 + 本机直连回退”，当主网关返回网络失败或 5xx 时会自动尝试 `VITE_REAL_TRADING_DIRECT_URL`（默认 `http://127.0.0.1:8000/api/v1/real-trading`），用于本地调试或网关短暂不可达场景。
- **启动前门禁**: `preflight` 会在启动前检查 K8s/Redis/DB/镜像/内部密钥，关键项不通过时阻止发起启动请求。
- **策略页监控联动（2026-03-19）**: `preflight` 返回 `checked_at` 与 `checks[].details`，策略管理页会直接消费这些结果渲染“运行环境 / 传输连接”状态，不再依赖静态占位文案。
- **预测排名准备度门禁（2026-03-10）**:
  - 新增 `getTradingPrecheck(tradingMode)`，调用 `/api/v1/real-trading/trading-precheck` 检查“当前生产模型 + 当日特征”是否已就绪；
  - 新增 `TradingPrecheckResult/TradingPrecheckItem` 类型；
  - 新增 `extractTradingPrecheckFailure`，可从启动接口 `409` 结构化失败中提取 `items[]` 供 UI 回显。
- **闭环诊断**: `preflight` 额外回传 Stream 闭环探针（`stream_series_freshness` / `stream_quote_persist_rate` / `stream_kline_fetch`），并在 `PreflightCheckItem.details` 中提供结构化明细。
- **账户口径收敛（2026-04-09）**:
  - `getAccount/getRuntimeAccount` 对应的实盘账户快照现在优先消费 PostgreSQL 视图返回的 `payload_json.positions` 与 `position_count`，并将 `today_pnl`/`daily_pnl` 口径与 `day_open_equity` 对齐；
  - `initial_equity` 已从 PostgreSQL 基线表统一读取，并会作为实盘页面顶部“初始资金”展示口径；
  - 新增 `getAccountLedgerDaily(days, userId, tenantId, accountId?)`，对接 `/api/v1/real-trading/account/ledger/daily`；智能图表优先使用日账本快照中的 `daily_return_pct` 生成每日收益率序列，减少短时快照抖动和金额口径混入导致的曲线跳变；
  - 日账本接口现补充规范化派生字段 `daily_pnl/monthly_pnl/total_pnl/floating_pnl` 与语义标记 `snapshot_kind/broker_today_pnl_raw`，上层展示应优先使用这些字段，不再直接把 `*_raw` 当作用户展示口径；
  - `/account` 与 `/ledger/daily` 现统一补充 `daily_return_pct/total_return_pct`（百分数字面值）与 `daily_return_ratio/total_return_ratio`（比例值），并通过 `baseline` 对象收敛 `initial_equity/day_open_equity/month_open_equity`；
  - `QMT Agent` 在线预检统一把无时区的 `snapshot_at` 按 UTC 解释，避免把最新快照误判为过期。
- **风控参数下发**: `start` 支持附带 `execution_config`（如 `max_buy_drop/stop_loss`），用于覆盖本次启动的执行风控快照。
- **实盘执行参数向导（2026-03-13）**:
  - `start` 新增支持附带 `live_trade_config`（JSON），承载调仓周期、执行时段、买卖时间点、委托方式、单轮最大委托数等参数；
  - `getStatus` 新增兼容读取 `live_trade_config`，用于实盘页回显当前运行中的调度参数；
  - `start` 响应新增 `effective_live_trade_config`，与 `effective_execution_config` 一并作为本次启动快照回显。
- **生效参数回显**: `start` 响应新增 `effective_execution_config`，`getStatus` 兼容读取 `execution_config` 供页面持续回显。
- **模拟盘资金口径**: `getSimulationDailySnapshots` 读取后端 `simulation_fund_snapshots`（数据库日快照），用于“当前现金/资金历史”等表数据口径展示；`getSimulationAccount` 仍用于读取 Redis 实时态账户结构（持仓、市值等）。
- **身份对齐修复（2026-03-09）**: 为避免 `Forbidden user_id override`（403），`realTradingService` 的实盘控制面请求（`preflight/start/stop/status/logs/account`）不再强制透传前端 `user_id/tenant_id`，统一由 JWT 身份解析执行主体。
- **统一账户选择（2026-04）**: 新增 `getRuntimeAccount(userId, tenantId, runtimeMode)`，页面侧只需传入后端运行态即可由服务层统一路由到实盘/模拟账户接口；账户展示与模式判断已迁移到 `pages/trading/utils/accountAdapter.ts`，不再由各页面自行拼接双轨口径。
- **实盘未绑定降级（2026-04-11）**:
  - `realTradingService.getAccount()` 现在会先查询 `GET /api/v1/internal/strategy/bridge/binding/status`；
  - 若当前用户未绑定 QMT 实盘账号，或已绑定但尚未上报账户快照，则直接返回“空实盘口径”对象，不再继续请求 `/api/v1/real-trading/account` 触发反复 `404`；
  - 返回对象会带 `message/account_unavailable_reason`，供页面区分 `unbound/not_reported`。
- `portfolioService.getFundOverview()` 已与账本规范对齐：
  - 初始权益优先读取 `baseline.initial_equity`；
  - `todayPnL` 优先读取规范化 `daily_pnl`；
  - `dailyReturn` 优先读取 `daily_return_ratio / daily_return_pct`，`totalReturn` 优先读取 `total_return_ratio / total_return_pct`，最终再回退基线推导；
  - 当旧兼容字段返回 `0`、但基线与盈亏可推导出非零收益率时，会优先采用推导值，避免“总盈亏非零但收益率显示 0.00%”；
  - 当显式收益率字段与 `total_pnl / daily_pnl + baseline` 推导结果明显冲突时，会优先采用同口径推导值，避免“总盈亏与总收益率不是一套账”；
  - 服务层直接返回百分数字面值（例如 `1.23` 表示 `1.23%`），组件层不再自行乘除换算。

### Portfolio Service

负责获取资金概览、持仓、绩效数据。

- **主要方法**: `getFundOverview`, `getSimulationAccount`, `listPortfolios`
- **降级策略**: 当后端不可用（网络错误/5xx）时，`getFundOverview` 会自动降级返回默认的模拟账户数据（初始资金 100万），并标记 `isSimulated: true`。
- **账户优先级（2026-03-09）**: 当调用 `getFundOverview(..., mode='real')` 时，服务层会优先读取实盘账户；若实盘账户未接入/未上报，则自动回退模拟账户数据，保证仪表盘持续可读。
- **资金口径修复（2026-03-09）**:
  - 资金字段解析改为“空值与 0 值可区分”，内部从 `||` 迁移为数值安全解析，避免 `cash=0` 被误回退为总资产；
  - 实盘可用资金兼容 `cash/available_cash/available_balance` 多字段口径；
  - 模拟盘 `initialCapital` 优先读取 `simulation/settings.initial_cash`，收益率按真实初始资金计算；
  - `monthlyPnL` 仅在后端明确返回时透传，前端不再使用估算值。
  - 实盘模式下若 `/real-trading/account` 返回 `is_online=false`（未上报），保持实盘口径返回 0，不再自动回退模拟盘。
- **资金指标可用性标记（2026-03-23）**:
  - `getFundOverview` 优先消费后端 `account.metrics + account.metrics_meta`（若存在）；
  - 新增 `todayPnLAvailable/totalPnLAvailable/totalReturnAvailable/monthlyPnLAvailable`，用于区分“指标未知”与“真实为 0”；
  - 新增 `initialCapitalEstimated`：实盘缺少基线初始权益时，会临时使用当前总资产并标记为估算；当前实现会优先读取 `account.initial_equity` 作为基线初始权益；
  - 新增 `metricsSource/metricsMeta` 透传，便于 UI 与日志定位“券商原值”或“服务端推导值”来源。
  - `winRate` 优先读取后端 `metrics.win_rate`，无该字段时才回退 `account.win_rate`，修复实盘账户胜率长期显示 `0.0%`。

### Trading Service

负责订单管理和交易记录。

- **主要方法**: `getRecentTrades`, `listOrders`, `createOrder`, `cancelOrder`
- **交易记录闭环（2026-03-19）**:
  - `getOrders(userId, status, tradingMode, options)` 现在支持传入 `startDate/endDate/limit/offset`，并会按 `tradingMode` 自动切换到 `/api/v1/orders` 或 `/api/v1/simulation/orders`；
  - 实盘/模拟盘交易记录页可按时间范围直接向后端查询，导出“全部筛选结果”不再依赖前端当前页快照。
- **实时记录口径修复（2026-03-09）**:
  - `getRecentTrades` 已改为“成交优先”：先读 `/api/v1/trades`，映射真实成交流水；
  - 成交接口不可用时自动降级读 `/api/v1/orders`，并通过 `isFallbackToOrders` 标记降级态，便于 UI 提示“当前为委托降级视图”。
- **模式联动**:
  - `getRecentTrades(limit, tradingMode)` 会将 `trading_mode` 透传到后端过滤（`real/simulation`），避免实盘与模拟盘混合展示。
- **状态兼容**:
  - 订单降级映射支持大小写状态（如 `FILLED/filled`、`PARTIAL/PARTIALLY_FILLED`）；
  - 无法识别状态显示为 `未知状态`，不再误归类为 `待成交`。
- **交易统计口径（2026-03-09 更新）**:
  - `getTradeStats` 优先解析后端 `/api/v1/trades/stats/summary` 扩展字段 `daily_counts[]`（`timestamp/value/label`）用于图表时序渲染。
  - 兼容旧响应结构：若后端仅返回汇总字段或结构不匹配，前端稳定降级为空数组，不再抛解析异常。
- **降级策略**: 当后端不可用时，`getRecentTrades` 返回空列表，并标记 `isOffline: true`。
- **404 熔断**: 当 `/api/v1/orders` 返回 404（网关未接路由）时，`listOrders` 会短时熔断 5 分钟并返回空数组，避免控制台持续刷屏。
- **兼容策略**:
  - `listOrders` 同时兼容数组响应与分页对象响应（`Order[]` / `{ orders, total, ... }`）。
  - 订单状态兼容 `partial` 与 `partially_filled`，并支持 `expired` 映射。
  - `cancelOrder` 默认携带 `{ order_id }` 请求体，兼容后端取消接口校验。

### Strategy Service

负责策略列表、策略启停和回测调用。

- **主要方法**: `getStrategies`, `startStrategy`, `stopStrategy`, `getBacktestHistory`
- **仪表盘同步修复（2026-03-20）**:
  - `getStrategies` 新增 `StrategyListResponse` 直出结构识别（`{ total, strategies }`）；
  - 当后端未返回 `code/success` 但列表结构有效时，前端不再误判为失败（此前会落成 `code=500`，导致“有运行中策略但仪表盘显示 0”）。
- **降级策略**: 当策略列表接口失败时，返回带“(模拟)”标识的本地降级数据，保障仪表盘可用性。
- **字段兼容**:
  - 支持 `strategy_id/strategy_name` 映射到统一 `id/name`。
  - 支持状态值兼容映射（如 `active/live_trading -> running`，`inactive -> paused`）。
  - 支持错误监控字段透传：`error_code`、`error_message`、`last_failed_at`。
- **状态统一（2026-03-09）**:
  - 兼容读取后端新增字段：`base_status`、`runtime_state`、`effective_status`。
  - 前端展示态优先采用 `effective_status`，并新增 `starting` 状态解析。
  - 后端未升级时继续回退旧 `status` 字段，保持兼容。

### Backtest Service

- **主要方法**: `runBacktest`, `pollStatus`, `getResult`, `optimizeQlibParameters`, `getQlibHealth`
- **CSV 导出口径统一（2026-04-02）**:
  - `exportCSV` 的本地兜底生成逻辑已对齐为“快速回测交易流水 8 列”格式（`日期/代码/方向/成交价/成交量/成交金额/手续费/权益余额`）；
  - 当后端导出路由临时不可用时，历史导出与快速回测导出仍保持同一 CSV 列结构，避免双口径。
- **默认调仓比例统一（2026-03-29）**:
  - `backtestService` 与 `backtestCenterService` 内置 TopkDropout 默认参数调整为 `topk=50, n_drop=10`；
  - 与策略模板口径保持一致：默认按 20% 调仓比例执行。
- **参数映射修复（2026-03-08）**: `runBacktest` 会将前端 `strategy_params` 中的费率与动态仓位字段提升为后端顶层字段（`buy_cost/sell_cost/dynamic_position/market_state_symbol/market_state_window/strategy_total_position/style`），避免参数仅停留在 `strategy_params` 导致后端不生效。
- **WebSocket 鉴权对齐（2026-03-08）**: `connectProgress` 连接 `/ws/backtest/{id}` 时会自动附带 `token + tenant_id + user_id` 查询参数，与引擎侧 WS 鉴权与归属校验规则保持一致。
- **422 修复**: `pollStatus` 请求已补充必填 `tenant_id`，避免状态轮询阶段触发 `Request validation failed`。
- **对比接口修复**: `compareBacktests` 已补充 `tenant_id` 参数，修复策略对比模块 `422 Request validation failed`。
- **错误可读性**: 对 FastAPI 422 的 `detail` 数组做首条字段提取，前端错误提示更具体（如 `body.tenant_id: field required`）。
- **轮询策略（无超时）**: `pollStatus` 不再对 `pending/running` 设置等待时限；前端会持续轮询直到后端返回 `completed/failed`。对于网络抖动或短暂 `5xx` 会自动重试，`401/403/404` 等终态错误仍会立即失败。
- **默认费率口径**: 前端默认佣金费率统一为 `0.00025`（万2.5），并按后端真实分项费率提交 `min_commission/stamp_duty/transfer_fee/min_transfer_fee/impact_cost_coefficient`。
- **历史 user_id 兼容**: `getHistory` 已支持 `default_user`、纯数字 ID（如 `1`）和零填充 ID（如 `00000001`）自动回退，避免“有数据但历史页显示 0 条”。
- **参数优化健康门禁**: 新增 `getQlibHealth()` 读取 `/api/v1/qlib/health`，前端可据 `redis_ok` 判断队列是否可用并禁用优化启动按钮。
- **异步优化结果口径**: 参数优化统一走异步任务（`/optimize?async_mode=true` + `/task/{id}/status`），轮询成功后返回完整 `all_results/best_params/target_metric` 供结果摘要与“一键回填”使用。
- **参数优化身份与停止控制（2026-03-10）**:
  - `optimizeQlibParameters` 提交优化请求时使用当前登录用户身份，不再透传历史占位 `user_id`。
  - 新增 `OptimizationProgressOptions.signal`，支持 UI 在点击“停止优化”后中断轮询。
  - 前端停止按钮会先调用 `/task/{task_id}/stop` 再中断本地轮询，避免 Celery 任务继续后台占用资源。
- **参数优化历史与恢复（2026-03-10）**:
  - 新增 `getOptimizationHistory()` 与 `getOptimizationDetail()`，读取后端持久化的网格搜索历史与详情。
  - 新增 `OptimizationHistoryItem/OptimizationHistoryDetail/OptimizationRunStatus` 类型，统一承载运行态与历史态。
  - 新增 `watchOptimizationTask()`，页面刷新或重新进入参数优化模块时可继续观察已有 `task_id` 的后台执行状态。
  - `pollOptimizationTask()` 现在会透传细粒度进度字段：`completed_count/failed_count/total_tasks/current_params/best_params/optimization_id`。

### Advanced Analysis Service

- `advancedAnalysisService.analyzeBasicRisk` 增加响应清洗：对 `NaN/Inf/undefined` 统一降级为 `0`，避免高级分析卡片显示 `NaN%`。
- 基础风险指标前端类型同步移除 `information_ratio`，与后端最新可计算口径保持一致。
- 交易统计类型新增 `profit_loss_days_ratio`（盈亏天数比），用于替代前端“盈亏比”主展示，减少无真实单笔 `pnl` 时的语义偏差。

## 测试

使用 Vitest 进行单元测试：

```bash
npm run test
# 或只运行服务测试
npx vitest run src/services/
```

## 仪表盘刷新协调器
- 入口：`refreshOrchestrator.requestRefresh(moduleId, reason)` / `requestAll(reason)`。
- 典型触发：
  - 页面重新可见（`visibilitychange`）
  - 路由回到仪表盘
  - 低频兜底轮询（120s）
  - 模块内用户操作后定向刷新
- 设计目标：只在数据变化时刷新 UI，避免各模块高频自刷新导致的观感抖动。

## WebSocket 连接约定
- 心跳协议与后端 `ws_core` 对齐：客户端发送 `type: "ping"`，服务端返回 `type: "pong"`。
- 连接建立采用并发保护：同一时刻重复调用 `connect()` 会复用同一 Promise，避免创建多条并发连接导致状态抖动。
- 鉴权前置：若本地不存在 `access_token/auth_token`，客户端会跳过 WS 建连（不触发 403 重连风暴）。
- 自动重连（2026-04-11）：全局 `websocketService` 在非主动断链场景下会每 30 秒重试一次，并在重连成功后自动补发已登记的 `symbols/channels` 订阅，避免页面刷新前行情主题丢失。

- `aiStrategyService.ts` 已继续拆分为 `aiStrategyClients.ts`、`aiStrategyServiceHelpers.ts`、`aiStrategyServiceFiles.ts`，主服务仅保留生成/回测/核心查询逻辑。

- `pages/modelRegistryUtils.ts` 与 `pages/modelRegistryPanels.tsx` 用于拆分模型注册页的纯函数和展示组件。
