# quantmind-trade

交易核心服务（订单、成交、持仓、模拟盘、风控）。

## 路由拆分（2026-04-08）

- `routers/real_trading.py`：聚合入口
- `routers/real_trading_utils.py`：身份、账户快照、预检写回、配置归一等工具
- `routers/real_trading_preflight.py`：`/preflight`、`/trading-precheck`、`/preflight/snapshots/daily`、`/account`
- `routers/real_trading_lifecycle.py`：`/start`、`/stop`、`/status`、`/logs`、`/orders`、`/history`
- `routers/manual_executions.py`：实盘页“手动执行调试”任务入口，支持创建任务、按任务类型查询任务、查询任务级日志
- `routers/internal_strategy.py`：聚合入口
- `routers/internal_strategy_utils.py`：内部网关共享 helper、模型、会话上下文
- `routers/internal_strategy_bridge.py`：`/dispatch/item-status` 与 `/bridge/*`
- `routers/internal_strategy_lifecycle.py`：`/heartbeat`、`/sync-account`、`/order`、`/hosted-executions`
- 拆分后补齐了 `internal_strategy_bridge.py` 对 `internal_strategy_utils.py` 的显式私有符号导入（`_bridge_ws_url`、`_agent_template_root`、`_qmt_agent_release_manifest_key`、`_qmt_agent_release_asset_ttl`、`_qmt_agent_release_local_manifest_path`、`_load_qmt_agent_release_manifest`、`_build_qmt_agent_release_asset`、`_iso_or_none`、`_to_float`、`_compute_account_metrics`、`_get_bridge_session_context`），避免 `import *` 跳过前导下划线名称导致的启动期/运行期 `NameError`。
- `_sync_qmt_account_to_db` 保持为 `internal_strategy_bridge.py` 本文件内的局部实现，用于账户快照入库，不从 utils 重复导入。

## 手动执行任务（2026-04-12，引导式预案版）

- 手动任务已从“一页式直接执行”重构为“预览 -> 确认提交”两阶段：
  - `POST /api/v1/real-trading/manual-executions/preview`：只做调仓预案计算，不落库、不发单；
  - `POST /api/v1/real-trading/manual-executions`：基于 `preview_hash` 二次校验后创建正式任务；
  - `GET /api/v1/real-trading/manual-executions`
  - `GET /api/v1/real-trading/manual-executions/{task_id}`
  - `GET /api/v1/real-trading/manual-executions/{task_id}/logs`
- 首版引导式执行仅支持 `REAL`，且只允许同租户/同用户的已完成推理批次 + 已验证策略。
- `preview` 会固定读取 `engine_signal_scores` 与最新实盘账户快照，生成结构化调仓预案：
  - `account_snapshot`
  - `strategy_context`
  - `sell_orders / buy_orders / skipped_items`
  - `summary`
  - `preview_hash`
- 预案计算语义：
  - 根据策略参数与当前持仓生成“先卖后买”的委托草案；
  - 对无持仓卖出、缺参考价、预算不足等情况，不生成委托，改记入 `skipped_items`；
  - 买入数量按 A 股 100 股整手和可用现金预算计算，不再固定写死 100 股。
- 正式执行语义（2026-04-23 更新）：
  - 执行阶段改为“先提交卖单，再进入买单”；
  - 卖单提交后会进入成交等待门控：默认最多等待 300 秒（`MANUAL_TASK_SELL_WAIT_TIMEOUT_SEC`），轮询间隔默认 3 秒（`MANUAL_TASK_SELL_WAIT_POLL_SEC`）；
  - 等待结束后按“初始可用现金 + 卖单实际成交金额”重算买单数量，再提交买单，减少因卖单未成交导致的买单资金不足拒单。
- 正式提交后，任务仍写入 `trade_manual_execution_tasks`，并由 `manual_execution_worker.py` 后台轮询消费，继续复用现有日志流 `qm:real-trading:manual-execution:*`。
- 当前 `completed` 仍表示“派单完成/已完成提交尝试”，不等价于柜台最终成交；最终成交仍以后续订单状态和执行回报流为准。
- 启动阶段会通过 `manual_execution_persistence.ensure_tables()` 自动补齐 `trade_manual_execution_tasks.progress` 列，兼容旧环境中缺少该列但运行期已读写进度值的历史表结构。
- 手动任务正式执行阶段已切换为“Agent 查价后转保护限价单”：
  - trade 服务下发 `order_type=MARKET`、`price=0`，并附带 `agent_price_mode=protect_limit`
  - QMT Agent 在临下单前按买一/卖一取 Level1 价格，并按 `MANUAL_TASK_AGENT_PROTECT_PRICE_RATIO`（默认 `0.002`）加减保护后，仍以 QMT `FIX_PRICE` 限价单提交
  - trade 风控估值不再依赖 preview 阶段的 Redis 参考价，而是通过 `QMTBridgeBroker.query_quote()` 实时调用 stream `GET /api/v1/quotes/{symbol}` 获取最新价做市值估算

## 自动托管任务化收口（2026-04-13）

- 自动托管已不再由 `runner/main.py` 逐笔调用 `/api/v1/internal/strategy/order`，而是统一改为：
  - runner 仅保留时间窗、触发检测、幂等锁与任务上报；
  - 命中条件后一次性调用 `POST /api/v1/internal/strategy/hosted-executions`；
  - trade 服务直接查询当前用户的默认模型最新完成推理结果，不再依赖 signal stream、fallback matcher 或外部信号作为数据源；
  - trade 服务按 `data_trade_date` + 默认模型 `target_horizon_days` 计算可执行窗口，超过窗口直接拒绝；
  - trade 服务基于默认模型最新推理结果、当前账户快照与策略参数生成 `execution_plan`，写入 `trade_manual_execution_tasks`；
  - 后续仍由 `manual_execution_worker.py` 异步消费，并复用手动任务执行器、日志流、QMT Agent 保护限价能力。
- `trade_manual_execution_tasks` 已扩展并兼容以下字段：
  - `task_type`：`manual | hosted`
  - `task_source`：如 `manual_page | hosted_runner`
  - `trigger_mode`：`manual | schedule`
  - `trigger_context_json`
  - `strategy_snapshot_json`
  - `parent_runtime_id`
- `GET /api/v1/real-trading/manual-executions` 已支持按 `task_type/task_source/active_runtime_id` 过滤。
- `GET /api/v1/real-trading/status` 已新增：
  - `latest_hosted_task`
  - `latest_signal_run_id`
  - `signal_source_status`
- `signal_source_status` 现在会区分默认模型状态来源：
  - `missing`：没有默认模型或没有最新完成推理
  - `window_pending`：已找到最新完成推理，但尚未进入可执行窗口
  - `expired`：已超过可执行窗口
  - `fallback` / `mismatch`：兜底或来源不匹配，自动托管拒绝
  - `user_default`：可用于自动托管
- 启动前 readiness 已增加自动托管上游检查：
  - `signal_pipeline_enabled`：当前用户默认模型最新完成推理是否可用于自动托管
  - `latest_signal_run`：当前用户默认模型最新完成推理批次号

> 注：自动托管链路当前接受“用户默认模型的最新 `completed` 推理结果”，其中 `model_source` 允许为 `user_default` 或 `explicit_system_model`；且要求该结果未使用兜底模型。T+5 之类的执行有效期由模型元数据中的 `target_horizon_days` 决定。

## 仪表盘口径收敛（2026-04-09）

- `GET /api/v1/real-trading/account` 现在只读 PostgreSQL 视图 `real_account_snapshot_overview_v`，会一并返回 `payload_json.positions`、`position_count`，并基于 PostgreSQL 的 `day_open_equity` 视图口径补齐 `today_pnl`；账户未持久化时返回 `404`，不再回退缓存快照。若视图尚未创建，服务会先回滚当前会话再降级到表查询，避免事务被错误状态污染。
- 实盘账户基线已拆分为独立表 `real_account_baselines`：新账户会在第一条 `qmt_bridge` 落库记录时自动写入初始资金，若存在人工修正则以基线表为准。`79311845 / 8886664999` 已被手动修正为 `21000000.00`。
- 新增实盘账户日账本 `real_account_ledger_daily_snapshots`：由 `qmt_bridge` 每次上报落库，保存 `initial_equity/day_open_equity/month_open_equity/today_pnl_raw/monthly_pnl_raw/total_return_pct` 等按日快照字段；`GET /api/v1/real-trading/account/ledger/daily` 默认按当前账户读取最近 N 天游标，并优先锁定当前活跃账户，避免多账户混读。
- 日账本规范化补层（2026-04-11）：
  - 持久化层继续保留兼容字段，但 `today_pnl_raw` 已回归为桥接层原始当日盈亏上报值，不再写入派生日盈亏；
  - `/api/v1/real-trading/account/ledger/daily` 现补充规范化字段：`snapshot_kind/broker_today_pnl_raw/daily_pnl/monthly_pnl/total_pnl/floating_pnl/daily_return_ratio/total_return_ratio/baseline`；
  - `GET /api/v1/real-trading/account` 与 `GET /api/v1/real-trading/account/ledger/daily` 已统一语义：
    - `broker_today_pnl_raw`：桥接层原始当日盈亏上报值，仅用于审计/排障；
    - `daily_pnl/monthly_pnl/total_pnl/floating_pnl`：规范化展示字段；
    - `daily_return_pct/total_return_pct`：百分数字面值（例如 `1.23` 表示 `1.23%`）；
    - `daily_return_ratio/total_return_ratio`：比例值（例如 `0.0123`）；
    - `baseline.initial_equity/day_open_equity/month_open_equity`：统一基线口径；
  - 前端图表与后续上层逻辑应优先消费规范化字段或 `*_return_pct/*_return_ratio`，避免直接依赖 `*_raw` 字段做展示。
  - 新增异常快照保护：若 QMT Agent 在退出/重连过程中上报 `total_asset/cash/market_value` 全为 `0` 且无持仓的空快照，而库内已存在历史有效快照，则 trade 服务会拒绝该次快照写入 `real_account_snapshots` 与 `real_account_ledger_daily_snapshots`，避免把资金概览和当日账本覆盖成 `0 / -100%`。
  - 异常快照保护已扩展到“资产跳变”场景：若相较最近有效快照出现整张资产负债表断崖式下跌，或仍声明存在持仓但 `market_value` 丢失，则视为桥接层不完整上报并拒绝落库。
  - `GET /api/v1/real-trading/account` 读取最新快照时会自动跳过最近的“空尾快照”，优先回退到最近一条有效 PostgreSQL 快照；同时 `is_online` 改为基于 `snapshot_at` 时效判断，不再固定返回 `true`。
  - `POST /api/v1/internal/strategy/bridge/account` 在快照被 guard 拒绝时，不再继续把原始坏 payload 同步进 `portfolio/positions`；若存在最近有效快照，则改用该快照回填组合资金与持仓，否则直接跳过本次 portfolio 同步。
  - 新增“零总资产但持仓/市值非空”的异常快照保护：这类桥接上报会被视为不完整 payload，`/account` 读取和 `bridge/account` 写缓存都会统一拒绝，避免前端把坏快照展示成真实实盘口径。
  - guard 触发时会在 `metrics_meta.snapshot_guard` 中回写结构化审计信息，并输出服务端告警日志，便于线上复盘被拒绝的上报内容与回退来源。
  - `real_account_ledger_daily_snapshots` 现在只接受“时间上更新”的快照覆盖；迟到旧包不会再倒灌覆盖当天账本。
  - 新增后台任务 `real_account_ledger_settlement_task`：每个交易日 `15:05`（上海时间）对当日日账本做一次“无迁移版日终结算”，在原有日账本行上补写 `settlement_finalized/settlement_finalized_at/settlement_snapshot_count` 等审计元数据，并将来源标记为 `daily_settlement`。
  - `GET /api/v1/real-trading/account/ledger/daily` 已透出 `settlement_finalized/settlement_finalized_at/settlement_snapshot_count`，上层可以据此区分“盘中实时账本”和“收盘后已结算账本”。
- `QMT Agent 在线预检` 会把 PostgreSQL 中无时区的 `snapshot_at` 按 UTC 解释，避免把最新快照误判为过期。
- `GET /api/v1/real-trading/preflight` 中的 `QMT Agent 在线状态` 检查已补 JSON 解析容错，避免脏快照把启动前自检直接打断。
- `GET /api/v1/real-trading/status` 的 `sys_` 模板回查改为懒加载并捕获异常，避免模板缺失导致状态轮询直接 500。

## 修复记录（2026-03-28，Runner 镜像预检与默认回退对齐）
- 修复 `/api/v1/real-trading/preflight` 与 `/api/v1/real-trading/trading-precheck` 在 `STRATEGY_RUNNER_IMAGE` 未显式配置时的误拦截问题。
- `/api/v1/real-trading/start` 会按模式恢复真实启动链路：`REAL/SHADOW` 通过 `k8s_manager` 拉起运行容器，`SIMULATION` 通过沙箱执行器提交任务；`/api/v1/real-trading/stop` 对应执行删除/停止。
- 预检输出会新增 `image_source=configured|default`，便于区分“显式配置”与“默认回退”。

## 修复记录（2026-04-02，健康检查降级语义对齐）
- `main.py` 启动阶段新增关键依赖状态跟踪：`startup_healthy`、`db_connected`、`redis_connected`。
- `/health` 现按综合状态返回：
  - `healthy`：启动与关键依赖均正常
  - `degraded`：任一关键依赖初始化失败
- `/health` 新增 `components.database/components.redis`，用于快速定位降级来源。
- 恢复并统一暴露：
  - `GET /` 根路由（`QuantMind Trade Core V2 is running`）
  - `GET /metrics`（Prometheus 服务健康指标）

## 修复记录（2026-04-13，bridge/account 日账本落库 KeyError）
- 修复 `real_account_ledger_service.upsert_real_account_daily_ledger()` 构造 upsert 行数据时遗漏关键字段的问题：
  - 补齐 `account_id`
  - 补齐 `snapshot_date`
  - 补齐 `last_snapshot_at`
- 修复后 `on_conflict_do_update` 不再触发 `KeyError: 'last_snapshot_at'`，`POST /api/v1/internal/strategy/bridge/account` 可继续完成账户快照与日账本落库闭环。

## 修复记录（2026-04-13，日收益率图表账本回填与查询增强）
- `GET /api/v1/real-trading/account/ledger/daily` 新增 `account_id` 参数，支持前端按实盘账户精确读取日账本，避免多账户/切换场景误读。
- 当账本表无数据但 `real_account_snapshots` 已有历史快照时，后端会自动执行一次按日回填（`qmt_bridge_backfill`），补齐 `daily_return_pct`/`total_return_pct` 等核心字段后再返回结果。
- 该回填能力可修复历史用户缺失日账本导致“智能图表无法读取每日收益率”的问题，同时保持后续桥接上报继续走实时持久化链路。

## 修复记录（2026-03-27，preflight 快照落库）
- 修复 `PreflightSnapshot` ORM 模型字段缺失问题（此前仅保留 `id/run_count`），与表 `real_trading_preflight_snapshots` 结构重新对齐。
- 修复后 `/api/v1/real-trading/preflight` 的日级快照 upsert 不再触发
  `Unconsumed column names`，可持续写入 `tenant_id/user_id/trading_mode/snapshot_date` 等留痕字段。
- 本次变更不需要新增迁移脚本：线上表结构已存在，属于代码侧模型定义回补。

## 实盘状态映射与最小申报股数修复（2026-03-23）
- QMT 执行回写状态码口径统一（Agent 与 Trade 服务一致）：
  - `50 -> SUBMITTED`
  - `56 -> FILLED`
  - `57 -> REJECTED`
- `POST /api/v1/internal/strategy/bridge/execution` 的 raw code 兜底映射同步更新：
  - `56` 按已成处理，`57` 按拒单处理。
- `POST /api/v1/internal/strategy/bridge/execution` 的成交入账口径已收口：
  - 仅带 `exchange_trade_id` 的 trade callback 会新增 `trades` 并累计 `orders.filled_quantity/filled_value`；
  - 不带 `exchange_trade_id` 的 order callback 只更新订单状态与 `exchange_order_id`，避免与后续 trade callback 双计成交。
- 风控新增 A 股买入整手校验（`RiskService`）：
  - 主板：`MIN_LOT_MAIN_BOARD`（默认 100）
  - 创业板：`MIN_LOT_GEM_BOARD`（默认 100）
  - 科创板：`MIN_LOT_STAR_BOARD`（默认 200）
  - 北交所：`MIN_LOT_BJ_BOARD`（默认 100）
- 校验不通过会直接返回风险阻断，避免无效委托进入柜台。

## 双向交易后端支持（2026-03-15）
- 新增固定融资融券股票池能力：默认读取项目根目录 `data/融资融券.xlsx`，通过 `backend.shared.margin_stock_pool` 提供只读判定。
- 订单/成交模型新增双向语义字段：
  - `trade_action`：`buy_to_open / sell_to_close / sell_to_open / buy_to_close`
  - `position_side`：`long / short`
  - `is_margin_trade`
- 组合/持仓模型新增信用口径字段：
  - `portfolios.liabilities / short_market_value / maintenance_margin_ratio / warning_level`
  - `positions.side / borrow_fee / financing_fee / margin_occupied / maintenance_margin_ratio`
- 风控新增最小双向规则：
  - `sell_to_open` 仅允许融资融券股票池内标的；
  - 校验保证金占用与 feature flag `ENABLE_MARGIN_TRADING`。
- Runner 与内部下单链路已支持透传 `trade_action / position_side / is_margin_trade`，前端暂不新增切换入口。
- `GET /api/v1/real-trading/preflight` 已补充双向交易专项检查：
  - `双向交易功能开关`
  - `融资融券股票池`
  - `信用交易动作支持`
  - `信用账户状态`

## QMT 多空实盘灰度（2026-03-19）
- 不新增外部下单契约字段，沿用：
  - `trade_action`
  - `position_side`
  - `is_margin_trade`
- Bridge 下单已透传三字段到 QMT Agent（含异步链路）。
- 新增服务端开关：
  - `ENABLE_LONG_SHORT_REAL=false`（默认关闭）
  - `LONG_SHORT_WHITELIST_USERS=`（逗号分隔）
  - `SHORT_ADMISSION_STRICT=true`（默认开启）
- REAL 模式 `sell_to_open` 风控新增标准错误码：
  - `LONG_SHORT_NOT_ENABLED`
  - `SHORT_POOL_FORBIDDEN`
  - `CREDIT_ACCOUNT_UNAVAILABLE`
  - `SHORT_QUOTA_INSUFFICIENT`
- `bridge/account` 快照结构已向后兼容扩展信用字段：
  - `liabilities`
  - `short_market_value`
  - `credit_limit`
  - `maintenance_margin_ratio`
  - `credit_enabled`
  - `shortable_symbols_count`
  - `last_short_check_at`
- CI 门禁已纳入以下必跑测试集（`scripts/ops/ci/p2_ci_quality_gate.py`）：
  - `backend/services/tests/test_qmt_agent_async_reconcile.py`
  - `backend/services/tests/test_trade_long_short_risk_and_bridge.py`
  - `backend/services/tests/test_trade_long_short_integration_chain.py`
  - `backend/services/tests/test_trade_trading_precheck.py`

### 迁移脚本
```bash
psql "$DATABASE_URL" -f backend/migrations/add_margin_trading_fields.sql
```

## 入口
- 应用入口：`/Users/qusong/git/quantmind/backend/services/trade/main.py`
- 默认端口：`8002`
- Kubernetes 命名空间：默认读取 `K8S_NAMESPACE`，未配置时使用 `quantmind`；`k8s_manager` 不再默认写死 `default`。
- 数据库配置来源：仅项目根目录 `.env`（服务目录 `.env` 不再维护 `DATABASE_URL`）。
- 生产 Redis 约定：实盘交易链路默认连接 `quantmind-trade-redis`，回测/通用链路连接 `quantmind-backtest-redis`，二者端口和数据独立。
- CORS 策略：开发/测试环境默认允许本机前端源；生产/预发环境必须通过 `CORS_ALLOWED_ORIGINS`（或兼容变量 `CORS_ORIGINS`）显式配置白名单，禁止 `*`。
- 镜像依赖策略：`trade` Docker 镜像改用 [requirements/trade.txt](/Users/qusong/git/quantmind/requirements/trade.txt)，不再安装 `requirements/ai.txt`，以降低镜像体积、冷启动时长和首启内存峰值。

## 模块边界（P1）
- `trade` 负责交易执行域：订单、成交、持仓、组合、模拟盘、实盘。
- 路由归属：`/api/v1/orders/*`、`/api/v1/trades/*`、`/api/v1/portfolios/*`、`/api/v1/simulation/*`、`/api/v1/real-trading/*`。
- 模拟盘账户管理统一实现位于 `backend/services/trade/services/simulation_manager.py`；
  `backend/services/trade/simulation/services/simulation_manager.py` 为兼容导出层，不再维护重复实现。
- 单条订单/成交读取与更新链路强制使用 `tenant_id + user_id + 业务ID` 约束，
  避免仅按主键读取导致的跨租户越权风险。
- `portfolios/positions` 单条查询与写操作强制绑定当前 `user_id` 作用域，
  禁止跨用户访问其他组合、持仓及快照数据。
- `real_trading` 相关接口改为以 JWT 身份为准，外部传入 `user_id/tenant_id`
  仅作一致性校验，禁止参数覆盖身份上下文。
- `/api/v1/real-trading/account` 错误语义收敛为标准 HTTP：账户未上报返回 `404`，服务异常返回 `500`，不再返回 `200 + {"error": ...}`。
- `portfolio` 模块已引入 `tenant_id` 字段并在查询/写入链路启用
  `tenant_id + user_id` 联合约束；迁移脚本见
  `backend/migrations/add_trade_portfolio_tenant_id.sql`。
- P3 可观测性基线：统一注入并透传 `X-Request-ID` 响应头，便于跨服务链路追踪。
- P3 错误契约：统一错误结构 `error.code/error.message/error.request_id`，并兼容保留 `detail` 字段。
- P3 日志基线：统一访问日志字段 `service/request_id/tenant_id/user_id/method/path/status/duration_ms`。
- P3 指标基线：新增 `/metrics`（Prometheus），统一暴露 `quantmind_service_health_status{service="quantmind-trade"}` 与 `quantmind_service_degraded{service="quantmind-trade"}`。
- P3 健康语义：`/health` 在数据库/Redis 等关键依赖初始化失败时返回 `status=degraded`，并同步到服务健康指标。
- P6 深度 E2E 修复：`simulation` 订单/成交模型的枚举字段改为按枚举值（小写）持久化，
  避免写入 PostgreSQL enum 时出现 `BUY/SELL` 与 `buy/sell` 不匹配。
- 禁止范围：`trade` 不负责策略生成/推理/回测，不承担行情采集与推送职责。

## 成交统计接口扩展（2026-03-09）
- 接口：`GET /api/v1/trades/stats/summary`（路径不变，字段扩展）。
- 返回新增：`daily_counts`（按 `executed_at` 日级聚合，升序输出，元素结构：`timestamp/value/label`）。
- 兼容保留：原汇总字段 `total_trades/total_value/total_commission/buy_trades/sell_trades` 继续返回。
- 聚合口径：严格按 `tenant_id + user_id + 可选 portfolio_id` 过滤，确保多租户与用户隔离。

## 实时交易记录接口兼容（2026-03-09）
- `GET /api/v1/trades` 已补充 `trading_mode` 大小写兼容：
  - 支持前端传入 `real/simulation` 或 `REAL/SIMULATION`；
  - 服务层统一归一化后过滤，非法值返回 `422 Invalid trading_mode`。
- 保持租户与用户隔离口径不变（鉴权上下文 `tenant_id + user_id`）。
- `GET /api/v1/orders` 与 `GET /api/v1/simulation/orders` 现已支持 `start_date/end_date/limit/offset`，前端交易记录页会按 `tradingMode` 自动切换到对应订单源，并将时间范围直接透传到后端查询。
- `GET /api/v1/orders` 与 `GET /api/v1/trades` 的时间查询参数新增时区兼容：当请求携带带时区的 ISO 时间（如 `...Z`）时，服务层会先统一转换为 UTC naive 时间，再与库内 `timestamp without time zone` 字段比较，避免 `asyncpg DataError: can't subtract offset-naive and offset-aware datetimes` 导致 500。
- `GET /api/v1/orders` 与 `GET /api/v1/trades` 的响应时间字段已统一序列化为带时区的 UTC ISO 字符串；即使旧客户端仍按浏览器默认方式解析，也能正确显示上海本地时间，避免仪表盘交易记录出现 `05:12` 这类偏移值。

## 模拟盘资金日快照（新增）
- 新增表：`simulation_fund_snapshots`（`tenant_id + user_id + snapshot_date` 唯一键）。
- 用途：按“天”永久保存用户资金概览，支持仪表盘资金历史追溯，不再仅依赖 Redis 当前态。
- 数据来源：`simulation:account:{tenant}:{user}`（Redis），按天 upsert 当日记录，保留历史天数据。
- 启动行为：`quantmind-trade` 启动后自动拉起后台任务周期采集（默认每 300 秒执行一次）。
- `/api/v1/simulation/account` 在账户不存在时会优先读取 `simulation/settings.initial_cash` 自动初始化，避免账户回退到默认 100 万导致设置与账户口径不一致。
- `/api/v1/simulation/reset` 与首次自动初始化都会立即触发一次当日资金快照采集，确保“保存/重置后”的历史资金口径及时落库。
- 新增接口：
  - `GET /api/v1/simulation/snapshots/daily?days=30`：查询当前用户日快照历史。
  - `POST /api/v1/simulation/snapshots/capture`：手动触发一次采集（按天 upsert）。
- 运行参数（根 `.env`）：
  - `SIM_FUND_SNAPSHOT_ENABLED=true|false`（默认 `true`）
  - `SIM_FUND_SNAPSHOT_INTERVAL_SECONDS=300`（默认 300 秒）
  - `SIM_FUND_SNAPSHOT_TZ=Asia/Shanghai`（默认上海时区）

## 实盘闭环执行链路（2026-02-25）
- 控制面：`/api/v1/real-trading/start|stop|status|logs` 负责启动/停止/观测实盘容器。
- 控制面新增：`/api/v1/real-trading/preflight` 启动前自检（K8s/Redis/DB/Runner 镜像/内部密钥），供前端做启动门禁。
- 控制面新增：`/api/v1/real-trading/preflight` 已补充 `QMT Agent 在线状态` 检测（基于 PostgreSQL 实盘账户快照 + `trade:agent:heartbeat:{tenant}:{user}` 心跳判定），`REAL` 模式下默认作为必需项。
- `RedisBroker.query_account` 已改为读取 PostgreSQL 视图 `real_account_snapshot_overview_v`，不再把 Redis 账户快照当作查询来源。
- 控制面新增：`/api/v1/real-trading/preflight` 已补充 Stream 闭环探针：
  `stream_series_freshness`（series 新鲜度）、
  `stream_quote_persist_rate`（quote 落库速率）、
  `stream_kline_fetch`（K线接口可用性），并通过 `checks[].details` 回传诊断明细。
- `MARKET_DATA_SERVICE_URL` 默认值已调整为 `http://quantmind-stream:8003`（容器内可达地址），避免在容器环境误用 `localhost:8003` 导致 K 线探针告警。
- 控制面新增：`/api/v1/real-trading/preflight` 已补充 `SIMULATION` 专用探针：
  `inference_database_ready`（模型推理数据库是否已具备前一交易日 48 维完整数据）、
  `simulation_sandbox_pool`（沙箱进程池存活）、
  `simulation_tables`（`sim_orders/sim_trades/simulation_fund_snapshots` 关键表）、
  `simulation_snapshot_worker_config`（资金快照任务配置，非阻断）。
- 模式收敛：`SIMULATION` 模式下 `preflight` 不再执行 Stream 相关探针（`stream_series_freshness/stream_quote_persist_rate/stream_kline_fetch`），仅返回模拟盘必要检查项，减少前端弹窗噪音。
- `stream_series_freshness` 数据源对齐：优先直连 `REMOTE_QUOTE_REDIS_*`（与 `quantmind-stream` 的 quote->series 写入端一致），远端探测异常时降级到交易 Redis 并在 `checks[].details.remote_probe_error` 回显原因。
- P3 兼容清理：`trade` 与 `portfolio/simulation` 的响应 schema 已切到 `ConfigDict(from_attributes=True)`，配置类切到 `SettingsConfigDict`，ORM 基类切到 `sqlalchemy.orm.declarative_base()`，避免 Pydantic V2 / SQLAlchemy 2.x 弃用告警污染 smoke/CI。
- 控制面新增：`/api/v1/real-trading/preflight` 调用后会自动写入
  `real_trading_preflight_snapshots`（按 `tenant_id+user_id+trading_mode+snapshot_date` 日级 upsert），用于长期留痕与问题追踪。
- 控制面新增：`GET /api/v1/real-trading/preflight/snapshots/daily?days=30&trading_mode=REAL`
  可查询当前用户的自检历史快照（含 `failed_required_keys/checks/last_checked_at`）。
- 控制面新增：`GET /api/v1/real-trading/trading-precheck?trading_mode=REAL|SHADOW|SIMULATION`
  用于检查“交易准备度”是否满足启动条件，返回 `{ passed, checked_at, items[] }`。
- `REAL/SHADOW` 仅保留 4 类核心项：
  生产模型存在、模型推理数据库已准备就绪（按前一交易日且 48 维完整判定）、Kubernetes 服务与执行镜像已就绪、实时行情服务已就绪；
  其中 `REAL` 额外要求 `QMT Agent` 心跳与 PostgreSQL 账户快照已上报，`SHADOW` 不阻断该项。
- 交易准备度检测前部固定追加 4 个基础必需项：
  `Redis`、`PostgreSQL`、`内部密钥`、`用户标识`，保持与原启动前自检的核心上下文一致。
- `MODELS_PRODUCTION` 未显式配置时，`trade` 默认按生产路径 `/app/models/production/model_qlib` 检测模型，
  避免误回退到历史相对路径 `model_qlib`。
- `SIMULATION` 的交易准备度会保留 4 个基础必需项（`Redis`、`PostgreSQL`、`内部密钥`、`用户标识`），并额外校验 `模型推理数据库已准备就绪`、`模拟盘进程池`、`实时行情服务已就绪`；其中模型数据库项也会出现在 `/api/v1/real-trading/preflight` 的模拟盘自检列表中，避免前端切换阶段后丢失模型相关结果。
- 控制面增强：`POST /api/v1/real-trading/start` 在 `REAL/SHADOW/SIMULATION` 全模式下
  会按模式执行启动门禁：
  `REAL/SHADOW` 先跑完整交易准备度检测，`SIMULATION` 先跑“基础 4 项 + 模型推理数据库 + 模拟盘进程池 + 实时行情”的最小准备度检测；
  未通过时返回 `409`，并在 `detail` 中附带 `precheck_failed/items/first_failed_reason`，供前端直接回显。
- 控制面新增：`/api/v1/real-trading/start` 支持前端传入 `execution_config`（JSON），会与策略默认风控合并后注入 runner `EXECUTION_CONFIG`。
- 控制面新增：`/api/v1/real-trading/start` 支持前端传入 `live_trade_config`（JSON），与策略模板默认实盘配置合并后注入 runner `LIVE_TRADE_CONFIG`。
- 控制面新增：`/api/v1/real-trading/start` 响应 `effective_execution_config/effective_live_trade_config`，`/api/v1/real-trading/status` 回传 `execution_config/live_trade_config` 用于前端持续回显。
- `GET /api/v1/real-trading/status` 在 `REAL/SHADOW` 场景也会稳定回传 `mode`（与 `SIMULATION` 分支对齐），避免前端顶部运行模式/部署通道落入“未识别”。
- `GET /api/v1/real-trading/status` 新增 `orchestration_mode`（`docker`/`k8s`），供前端准确展示部署通道，避免将 Docker 场景误标为 K8s。
- QMT Agent 接入收敛（2026-03-14）：
  - 仅保留 QMT Agent，PTrade 模板与入口已下线；
  - 新增 `POST /api/v1/internal/strategy/bridge/session` 与 `POST /api/v1/internal/strategy/bridge/session/refresh`，使用 `access_key + secret_key` 换取短期 `bridge_session_token`；
  - `/ws/bridge` 与 Agent 上报接口只接受短期 `bridge_session_token`，`qm_live_*` 不再直接用于 WebSocket 握手；
  - 新增 `POST /api/v1/internal/strategy/bridge/account`、`/heartbeat`、`/execution`，分别负责账户快照、心跳、执行回报闭环；
  - `POST /api/v1/internal/strategy/bridge/execution` 回写订单默认按 `tenant_id + user_id + client_order_id` 精确匹配；并新增兼容匹配链路（`client_order_id` 误传为 `order_id(UUID)` 或可用 `exchange_order_id` 时可回填），同时输出结构化日志便于排查回写键不一致；
- `QMTBridgeBroker` 下单派发成功后不再预填伪 `exchange_order_id`（不再回填 `client_order_id` 冒充柜台单号），真实柜台单号仅由 Agent 回报驱动更新；
- `TradingEngine.cancel_order_execution` 不再在发送撤单请求后直接写 `CANCELLED`，改为保留当前状态并写备注，最终状态由 QMT 回报落库；
- `tools/qmt_agent/qmt_agent.py` 已支持同步/异步下单撤单（`order_stock/order_stock_async`、`cancel_order_stock/cancel_order_stock_async`）与对应异步回报；
- Agent 启动后会执行一次 `query_stock_orders + query_stock_trades` 补偿查询并回写执行事件，降低断线窗口的状态缺口；
- `tools/qmt_agent` 侧已对 `access_key/secret_key/account_id/tenant_id/user_id` 等关键配置做统一 `strip()`，`download/agent` 生成的 `qmt_agent_config.json` 也会写入清洗后的值，避免复制粘贴带入换行或尾随空格导致 `401`；
- 新增 `GET /api/v1/internal/strategy/bridge/binding/status`，供前端设置中心展示 QMT Agent 在线状态；
  - `/api/v1/internal/strategy/bridge/download/agent` 继续要求登录态 JWT，返回 `qmt_agent_client.zip`，其中包含 `qmt_agent.py`、`desktop_app.py`、`requirements.txt`、`README.md`、`qmt_agent_config.json`、Windows 打包脚本（`build_windows_agent.py`/`qmt_agent_desktop.spec`/`qmt_agent_setup.iss`/`version.json`）和参考脚本；下载请求中的 `user_id` 若与登录用户不一致会返回 `403`。
  - 下载包完整性修复（2026-03-23）：`qmt_agent_client.zip` 现额外包含运行必需子模块 `agent.py/auth.py/client.py/config.py/reporter.py/_callback.py`，避免仅更新入口脚本时子模块仍停留旧版本。
  - `qmt_agent_client.zip` 的定位已收敛为“独立部署包源代码”，用于在客户服务端或专用 Windows 主机单独安装/打包 QMT Agent；Electron 只提供下载入口，不直接控制该 Agent 的本地生命周期。
  - 新增 `/api/v1/internal/strategy/bridge/download/agent/release`：后端优先读取 COS 上的 `qmt-agent/windows/release/latest.json`，返回 Windows 安装器的预签名下载地址，前端下载按钮改走该接口，不再直连 COS。
  - 推荐 COS 目录结构：
    - `qmt-agent/windows/release/latest.json`
    - `qmt-agent/windows/release/v{version}/QuantMindQMTAgent-Setup-{version}.exe`
    - `qmt-agent/windows/release/v{version}/QuantMindQMTAgent-{version}-win64.zip`
    - `qmt-agent/windows/release/v{version}/sha256.txt`
  - 相关环境变量：
    - `QMT_AGENT_RELEASE_MANIFEST_KEY`：发布清单 key，默认 `qmt-agent/windows/release/latest.json`
    - `QMT_AGENT_RELEASE_URL_TTL`：安装器签名 URL 有效期，默认 `1800` 秒
    - `QMT_AGENT_RELEASE_MANIFEST_LOCAL_PATH`：本地 manifest 兜底路径，便于开发联调
  - `tools/qmt_agent/build_windows_agent.py` 现在会在本地 `dist/qmt_agent/latest.json` 生成发布清单，包含版本、构建时间、COS key 和 SHA256，便于同步上传到 COS。
  - `tools/qmt_agent/qmt_agent.py` 已合并旧 `local_agent` 的关键执行能力：`xtquant` 实盘下单、QMT 自动重连、周期账户查询与心跳上报；旧 Redis Stream 终端直连方案不再作为正式交付面维护。
  - Redis 键规范（2026-04-09）：
    - 数字型 `user_id` 统一归一化为 8 位补零后再拼接 `trade:account` / `trade:agent:heartbeat`；
    - 账户快照、心跳、preflight、交易准备度、bridge broker、信用风控均已统一复用共享 key helper，避免出现“Agent 明明在线但某条读路径判离线”的假阴性。
  - 在线门禁统一收敛为 `qmt_agent_online`，依赖 PostgreSQL 实盘账户快照与 `trade:agent:heartbeat:{tenant}:{user}` 心跳判定；
  - Bridge 派发唯一连接（2026-04-09）：
    - `/ws/bridge` 握手元数据现显式带 `session_id`；
    - 同一 `binding_id` 建立新连接后，`stream` 会主动清理旧 bridge 连接；
    - `/api/v1/internal/bridge/order` 与 `/api/v1/internal/bridge/cancel` 仅向最新活动 bridge 连接派发，避免同账户多连接重复下单。
  - 在线联调已验证：旧的 `qm_live_*` 直连 `ws/bridge` 会被拒绝，新会话链路可以成功写入账户快照与心跳；
  - `bridge/account` 快照字段补齐：支持上报并透传 `today_pnl` / `total_pnl` / `floating_pnl`，用于前端优先展示柜台盈亏口径；
  - 持仓快照价格口径增强：`cost_price` / `last_price` 支持多字段兜底，缓解柜台回包缺字段导致前端盈亏恒为 0 的问题；
- 注意：当前交易服务读取的是交易 Redis **DB 2**，排查“无数据上报”时不要误查默认库 `0`。
- 内部下单热修（2026-03-14）：
  - 修复 `/api/v1/internal/strategy/order` 对 `TradingMode.SHADOW` 的硬引用问题。
  - 当前枚举不包含 `SHADOW` 成员时，旧逻辑会抛 `AttributeError: SHADOW` 并导致下单请求返回 `500`；
    已改为按 `trading_mode.value` 字符串判断，避免枚举成员缺失引发运行时异常。
  - 修复 `except IntegrityError` 未导入问题（`NameError: IntegrityError`），避免重复 `client_order_id` 或唯一键冲突时误报 `500`。
  - 交易枚举与线上 PostgreSQL enum 口径对齐：`orderside/ordertype/orderstatus` 统一使用库中小写值，并为接口输入保留大小写兼容解析（如 `BUY/buy`、`LIMIT/limit` 均可）。
  - 风控资金读取改为优先查询本地 `portfolios` 表（`tenant_id + user_id + portfolio_id`），远程 `/api/v1/portfolios/{id}` 仅做兜底，避免内部无 JWT 调用导致 `403` 后误判可用资金为 `0`。
  - Broker 适配兼容：`TradingEngine` 下单时按 Broker 类型转换 `side/order_type`（QMT 用大写，模拟盘用小写），并在 Broker 不支持 `client_order_id` 参数时自动跳过，避免 `unexpected keyword argument` 导致执行失败。
  - REAL 下单默认 Broker 已切换为 `bridge`：通过 `quantmind-stream` 内部接口将订单派发到在线 QMT Agent（`/ws/bridge`），不再默认依赖 `QMT_HOST:QMT_PORT` 本地 HTTP 直连。
  - 新增配置：`REAL_BROKER_TYPE`（默认 `bridge`，可选 `qmt`/`redis`）；`REAL` 下单若未传 `client_order_id`，会自动生成 `auto-<uuid>` 以保证 Agent 回报可关联订单。
  - 修复 `TradingEngine` 在 `begin_nested()` 中调用会 `commit` 的 service 导致事务关闭异常（`Can't operate on closed transaction...`）；Bridge 派发场景下改为仅提交“已派发”状态，真实成交统一由 `/bridge/execution` 回写落库。
  - `TradingEngine` 的 `asyncio.TimeoutError` 分支现在统一保留当前订单状态，并追加 `[AWAITING_BRIDGE_ACK]` / `[BRIDGE_ACK_TIMEOUT_PENDING_REVIEW]` 标记，避免把“派发后状态未知”的订单误写成终态失败。
- `/api/v1/real-trading/status` 容错增强：当 Redis 中 `trade:active_strategy:{tenant}:{user}` 为历史脏数据、非 JSON 或非对象结构时，不再直接返回 `500`，改为记录告警并按空状态继续返回。
- 同一接口在实盘运行时会额外返回当前组合快照 `portfolio`，并透出 `daily_pnl` / `daily_return`，供上层策略监控直接显示真实“今日收益”。
- 执行面：`runner` 通过内部网关 `/api/v1/internal/strategy/order` 上报信号。
- 执行回写新增：`POST /api/v1/internal/strategy/dispatch/item-status`（内部密钥保护）用于按 `client_order_id` 回写 `engine_dispatch_items` 与 `orders` 状态，支撑实盘 E2E 状态机闭环。
- 交易面：内部网关直接落单并调用 `TradingEngine` 执行（风控检查 -> Broker 下单 -> 订单/成交落库 -> Redis 账户回写）。
- 影子模式：`trading_mode=SHADOW` 在 `SimulationAccountManager` 做虚拟成交，不触发真实 Broker。
- 模拟结算器：`SimulationSettler` 已补齐目标权重到调仓股数（`delta_volume`）换算逻辑，避免结算阶段未定义变量导致中断。
- 身份与租户：内部网关统一从 `X-User-Id`/`X-Tenant-Id` 识别执行主体，缓存键统一带 `tenant_id`。
  - 旧链路下线：`dispatcher`（`trade:internal:order_queue`）与 `order_stream_consumer`（`trade:orders:*`）已移除，不再作为交易执行入口。
  - 账户盈亏口径增强（2026-03-23）：
- `POST /api/v1/internal/strategy/bridge/account` 会先将账户快照持久化到 PostgreSQL，再更新 Redis 缓存；`POST /api/v1/real-trading/account` 现在只读 PostgreSQL 视图 `real_account_snapshot_overview_v`，不再在 API 层拼接 Redis 补字段；
- 当券商未上报或上报为 0 时，服务端会基于数据库快照基线自动推导 `today_pnl/total_pnl/monthly_pnl/total_return`，并按持仓 `cost_price/last_price/volume` 推导 `floating_pnl`；
- 新增表 `real_account_snapshots`（ORM 定义位于 `backend/services/trade/models/real_account_snapshot.py`，按 `tenant_id/user_id/account_id/snapshot_at` 维度持久化），同时新增视图 `real_account_snapshot_overview_v`，统一输出最新快照与日/月/累计基线，避免 Redis 重启或覆盖导致口径漂移；
    - 首次启用时支持“上一交易日收盘权益”自动回填：当历史缺少前一日快照且券商上报 `today_pnl` 可用时，会自动写入 `source=auto_backfill_prev_close` 的回填快照，并作为今日基线；
    - `trade:account:{tenant}:{user}` 仅保留短期缓存与推送用途，不再参与账户概览页面展示决策；
    - `real_account_snapshot_overview_v` 负责提供 `today_pnl/total_pnl/floating_pnl` 以及 `initial_equity/day_open_equity/month_open_equity` 等展示口径，前端只消费 PostgreSQL 派生结果。
    - `payload_json` 会把桥接上报的柜台扩展字段（含 `positions` / `credit` / `metrics_meta`）一并落库，供风险校验、审计与 broker 兼容查询使用。
    - 口径一致性修复：`total_return` 与最终 `total_pnl` 同源计算（`broker_raw` 或 `db_snapshot`），避免“总盈亏为负但收益率显示 0.00%”。
    - 新增 `win_rate`（基于持仓盈利只数占比）随账户快照一并上报，供资金卡片“投资胜率”展示。
  - REAL 成交回写防覆盖（2026-03-23）：
    - `TradingEngine._sync_account_to_redis` 在 `recent_mode=REAL` 时不再覆盖 `trade:account`；
    - REAL 账户快照统一以 QMT Agent 的 `bridge/account` 为准，避免丢失扩展字段（如 `metrics/metrics_meta`）。
- **股票名称自动补全 (2026-03-23)**:
  - 新增 `backend/services/trade/utils/stock_lookup.py`：基于本地 `stocks_index.json` 的通用股票名称查询工具，支持 Docker 路径自适应。
  - `OrderService` 与 `InternalStrategyRouter` 已全链路接入该工具，确保报单、成交及持仓快照中的 `symbol_name` 字段在缺失时能自动补全。
  - `QMTPositionPayload` 协议已扩展 `symbol_name` 字段，支持持仓明细中的名称显示。
- Runner 地址规范：
  - 可直接设置完整地址 `TRADE_SERVICE_HEARTBEAT_URL` / `TRADE_SERVICE_ORDER_URL`
  - 或设置基地址 `TRADE_SERVICE_INTERNAL_URL`（自动拼接 `/heartbeat` 与 `/order`）

## 实盘发布端三层信号改造（2026-02-26）
- `runner` 执行端已切换为消费 `signal stream`（`qm:signal:stream:{tenant}`），不再在执行端主动调用 pipeline。
- runner 启动阶段云端模型下载默认后缀已切换为 `.txt`（`/app/model.txt`），与 `model_qlib/model.txt` 生产模型格式保持一致。
- 信号生产由 `engine` 侧发布（`ENABLE_SIGNAL_STREAM_PUBLISH=true`），执行端只做消费、风控和下单。
- 动态 runner 运行时地址统一：
  - `TRADE_SERVICE_INTERNAL_URL` 默认 `http://quantmind-trade:8002/api/v1/internal/strategy`
  - `ENGINE_SERVICE_INTERNAL_URL` 默认 `http://quantmind-engine:8001/api/v1`
  - 避免容器内误用 `127.0.0.1` 或遗留 `trade-core/engine-compute` 域名导致回写/查询失败。
- 发布行为：按融合得分排序生成订单，写入 `/api/v1/internal/strategy/order`；`remarks` 附带 `fusion_score` 便于审计。
- 调度方式：runner 在交易时段持续消费 Redis Stream（`XREADGROUP` 阻塞读取），不再按日触发“执行端再平衡”，循环内按 `heartbeat_interval_seconds` 持续心跳上报。
- 幂等防重：对每轮订单信号计算指纹并写入 Redis `SETNX` 锁（`idempotency_ttl_seconds`），重复信号自动跳过下单。
- 服务端幂等：runner 为每笔订单注入 `client_order_id`，`/api/v1/internal/strategy/order` 对重复 `client_order_id` 返回 `duplicate_skipped`，不重复创建订单。
- 信号归属隔离：runner 默认使用 `signal-runners-{tenant}-{user}-{strategy}` 独立 consumer group，避免多 runner 共享组导致串单；消费时按 `event.user_id` 二次校验。
- 执行回报入库消费：`ExecutionStreamConsumer` 订阅 `EXEC_STREAM_PREFIX:{tenant}`，处理 `order_submitted/order_rejected/order_filled`；
  - 订单匹配优先级：`order_id(UUID)` -> `tenant_id + user_id + client_order_id` -> `tenant_id + user_id + exchange_order_id` -> 兼容将 `broker_order_id` 解释为本地 `order_id(UUID)`；
  - `order_filled` 幂等键优先使用 `exchange_trade_id`；若上游未提供，再回退到 `broker_order_id + exec_id`，避免把 `broker_order_id` 错当本地 UUID 才能入账。
- 信号最新性闸门：runner 会读取 `qm:signal:latest:{tenant_id}:{user_id}`，仅接受当前最新 `run_id` 的 `qm:signal:stream:{tenant}` 消息；旧 run 或重放消息会被直接 ACK 丢弃，避免消费过期推理结果。
- 为避免“最新推理刚完成、runner 已读到旧快照”的短窗口，runner 在真正发单前会再次读取 `qm:signal:latest:{tenant_id}:{user_id}` 并做二次比对；若版本已变化，信号会在下单前被跳过，不会继续送往交易服务。
- `quantmind-trade` 服务启动时会自动拉起 `ExecutionStreamConsumer`（受 `ENABLE_EXEC_STREAM_CONSUMER` 开关控制），服务停止时自动优雅下线并完成已拉取批次的 ACK。
- 组合级风控闸门：下单前调用 `/api/v1/internal/strategy/sync-account` 获取真实资金/持仓，并执行
  `max_turnover_ratio_per_cycle`、`buy_cash_utilization`、`max_single_order_notional`、持仓卖出上限等约束。
- 执行风控补充：runner 支持 `max_buy_drop`（日内大跌买入拦截）与 `stop_loss`（达到阈值后仅允许卖出减仓）。
- 执行时机调度增强（2026-03-13）：
  - runner 读取 `LIVE_TRADE_CONFIG`，按 `rebalance_days/schedule_type/trade_weekdays` 判断是否为调仓日；
  - 按 `enabled_sessions + sell_time + buy_time + sell_first` 划分 `IDLE/SELL/BUY` 阶段，`SELL` 窗口仅允许卖单、`BUY` 窗口仅允许买单；
  - 每轮下单数量受 `max_orders_per_cycle` 限制，并在结构化日志中输出 `phase/is_rebalance_day/live_trade_config`。
  - 时区口径固定为 `RUNNER_TIMEZONE`（默认 `Asia/Shanghai`），避免容器默认 UTC 导致交易时段门禁误判为 `IDLE`。
- 临时联调开关（仅测试）：
  - 设置 `RUNNER_TEST_MODE=true` 后，runner 会绕过交易日/交易时段门禁，直接消费 Stream 并执行下单流程；
  - 验证结束后需立即关闭该开关并重启 runner，避免非交易时段误执行。
- 执行参数对齐补齐（2026-03-13）：
  - `execution_config.stop_loss` 已直接映射 runner 全局止损，不再依赖历史键 `global_stop_loss_drawdown`；
  - `execution_config.max_buy_drop` 已接入买入拦截逻辑，当行情跌幅低于阈值时跳过买单；
  - `live_trade_config.max_price_deviation` 已接入 runner 价格偏离校验，按前端设置限制信号价与实时价偏差。
- 交易时段调度：默认仅在工作日的 A 股连续竞价时段（09:30-11:30, 13:00-15:00）执行信号消费与下单；可通过 `ignore_trading_calendar=true` 覆盖。
- 日历引擎：默认启用 `use_exchange_calendar=true`，优先使用 `exchange_calendars` 的 `XSHG` 交易所日历做精确交易日/时段判断（节假日与盘中休市）；不可用时自动回退到工作日+固定时段规则。
- 容器环境新增：
  - `TENANT_ID`：由控制面注入，内部调用统一带 `X-Tenant-Id`
  - `EXECUTION_CONFIG` 支持：
    `max_orders/notional_per_order/lot_size/idempotency_ttl_seconds/`
    `max_turnover_ratio_per_cycle/buy_cash_utilization/max_single_order_notional`
    `heartbeat_interval_seconds/off_market_sleep_seconds`
  - `RUNNER_ENABLE_EXEC_STREAM_PUBLISH`：默认 `true`，将下单执行回报写入 Redis Stream。
  - `LIVE_TRADE_CONFIG`：控制调仓周期、执行时段、买卖时间点、委托方式与单轮最大委托数。
  - `EXEC_STREAM_PREFIX`：默认 `qm:exec:stream`。
  - `SIGNAL_STREAM_PREFIX`：默认 `qm:signal:stream`。
  - `SIGNAL_STREAM_GROUP`：可选；不填时默认 `signal-runners-{tenant}-{user}-{strategy}`。
  - `SIGNAL_STREAM_CONSUMER_NAME`：可选；不填时默认 `runner-{tenant}-{user}-{strategy}`。
  - `SIGNAL_STREAM_BATCH_SIZE`：默认 `100`。
  - `SIGNAL_STREAM_BLOCK_MS`：默认 `1000`。
- 安全回退：若三层闭环调用失败，runner 将停止本轮下单并保留心跳，不再发旧静态假信号。
- Deployment 命名规则：
  - runner Deployment 统一按 `strategy-{tenant}-{user}` 命名，并对超长名称做截断 + hash；
  - `start/stop/status/logs` 全链路统一使用 `tenant_id + user_id` 寻址，避免多租户下命名冲突或查错命名空间对象。
- 消费器开关与参数：
  - `ENABLE_EXEC_STREAM_CONSUMER`：默认 `false`（建议灰度开启）
  - `EXEC_STREAM_PREFIX`：默认 `qm:exec:stream`
  - `EXEC_STREAM_GROUP`：默认 `exec-trade`
  - `EXEC_STREAM_CONSUMER_NAME`：默认 `trade-consumer-1`
  - `EXEC_STREAM_BATCH_SIZE`：默认 `100`
  - `EXEC_STREAM_BLOCK_MS`：默认 `3000`
  - `EXEC_STREAM_TENANTS`：默认 `default`（多租户可逗号分隔）
  - `EXEC_STREAM_REDIS_DB`：默认 `0`（用于执行回报流消费，独立于 `trade` 主 Redis DB）
  - `EXEC_STREAM_MAX_RETRY`：默认 `3`（处理失败后重试次数）
  - `EXEC_STREAM_DLQ_PREFIX`：默认 `qm:exec:dlq`（重试耗尽/非法事件入死信）
- 订单超时扫描（防“submitted 假成功”）：
  - `ORDER_TIMEOUT_MINUTES`：默认 `30`，兜底将长时间无回报的 `REAL+SUBMITTED` 订单标记为 `EXPIRED`。
  - `ORDER_SCAN_INTERVAL_SEC`：默认 `300`，长超时扫描周期。
  - `BRIDGE_ACK_TIMEOUT_SECONDS`：默认 `20`，仅针对含 `[AWAITING_BRIDGE_ACK]` 且无 `exchange_order_id` 的桥接订单，超时追加 `[BRIDGE_ACK_TIMEOUT_PENDING_REVIEW]` 标记并推送“待核查”通知，不直接改写为 `REJECTED`。
  - `BRIDGE_ACK_SCAN_INTERVAL_SEC`：默认 `5`，桥接 ACK 短超时扫描周期。
  - `TradingEngine` 直接下单请求超时同样会走“待核查”语义，不会改写为 `REJECTED`，并保留桥接等待标记供扫描器兜底。

### DLQ 回放工具
```bash
# 1) 先 dry-run 看候选事件
python backend/services/trade/scripts/dlq_replay.py \
  --tenant default \
  --event-type order_filled \
  --reason-contains processing_error \
  --max-count 50 \
  --dry-run

# 2) 执行回放并删除已回放的 DLQ 消息
python backend/services/trade/scripts/dlq_replay.py \
  --tenant default \
  --event-type order_filled \
  --max-count 50 \
  --delete-after-replay
```

### P2-2 数据迁移/验收/回滚
```bash
# 迁移
psql "$PSQL_URL" -v ON_ERROR_STOP=1 -f backend/migrations/add_trade_portfolio_tenant_id.sql

# 验收
psql "$PSQL_URL" -v ON_ERROR_STOP=1 -f backend/migrations/verify_trade_portfolio_tenant_id.sql

# 回滚（仅当 tenant_id 全部为 default 时允许）
psql "$PSQL_URL" -v ON_ERROR_STOP=1 -f backend/migrations/rollback_trade_portfolio_tenant_id.sql
```

## 实盘 Runner 镜像构建（基于 Qlib 回测环境）
- 统一镜像 Dockerfile：`/Users/qusong/git/quantmind/docker/Dockerfile.ml-runtime`
- 默认镜像名：`quantmind-ml-runtime:latest`（与 `STRATEGY_RUNNER_IMAGE` 默认值语义一致）
- 实盘状态接口 `real_trading_lifecycle.py` 依赖 `real_trading_utils.py` 的 `_fetch_active_portfolio_snapshot` 等内部 helper；拆分后若新增状态字段，必须同步导入清单。

```bash
# 本地构建（默认安装 pyqlib）
docker build -f docker/Dockerfile.ml-runtime \
  -t quantmind-ml-runtime:latest \
  .

# 不安装 pyqlib（仅执行端消费信号/下单场景，可加快构建）
docker build -f docker/Dockerfile.ml-runtime \
  --build-arg SKIP_PYQLIB=1 \
  -t quantmind-ml-runtime:latest \
  .

# 面向 x86_64 服务器构建（推荐）
docker build --platform linux/amd64 -f docker/Dockerfile.ml-runtime \
  -t quantmind-ml-runtime:latest \
  .

# 构建并推送到镜像仓库
docker build --platform linux/amd64 -f docker/Dockerfile.ml-runtime \
  -t asia-east1-docker.pkg.dev/<project>/<repo>/quantmind-ml-runtime \
  .
```

### 构建参数
- `PIP_INDEX_URL`：默认 `https://mirrors.aliyun.com/pypi/simple/`，可切换为企业镜像源。
- `SKIP_PYQLIB`：默认 `auto`；设为 `1` 跳过 `pyqlib` 安装，设为 `0` 强制安装。

### 运行时关键说明
- 镜像已包含 `backend/shared`，保证 runner 可正常导入 `backend.shared.event_bus.schemas`。
- 旧 runner 入口 `/app/main.py` 已退役，仅保留兼容性警告；新的实盘执行必须通过手动任务或托管任务链路完成。
- runner 会优先直接读取 `INTERNAL_CALL_SECRET/SECRET_KEY`，避免镜像因认证模块额外依赖导致启动失败。
- 生产环境通过 `STRATEGY_RUNNER_IMAGE` 指向目标镜像 tag，当前主标签为 `quantmind-ml-runtime:latest`。
- 镜像内置 `HEALTHCHECK`：检查 `/tmp/heartbeat` 是否在 180 秒内更新（`interval=30s timeout=5s start-period=60s retries=3`）。

## 本次重构更新（2026-02-18）
- 移除 `main.py` 中历史 portfolio 服务的 `sys.path` 注入。
- 将 portfolio 相关实现本地化到 `backend/services/trade/portfolio/`：
  - `models/`
  - `schemas/`
  - `services/`
  - `utils/`
  - `config.py`
- `trade` 服务不再直接依赖历史 portfolio 模块。
- 修复 `deps.get_redis` 依赖注入，统一返回 `trade.redis_client.RedisClient`，
  避免模拟盘/订单链路出现 Redis 客户端类型不一致。
- 修复模拟盘与组合路由中的 `user_id` 类型对齐（JWT 字符串 -> 整型），
  避免写库/查询时出现 PostgreSQL 类型不匹配。
- 新增 `PositionService.sync_trade_update`，支持“成交同步持仓”内部链路。
- 数据库初始化 `init_db()` 已接入统一 Schema 注册中心
  `backend/shared/schema_registry.py`，一次性建表覆盖：
  - `trade.core`
  - `trade.simulation`
  - `trade.portfolio`

## Schema 归口巡检
```bash
source .venv/bin/activate
python backend/scripts/schema_registry_audit.py --schema trade.core --schema trade.simulation --schema trade.portfolio
```

## 测试
```bash
source .venv/bin/activate
pytest -q backend/services/tests/test_trade_service.py
pytest -q backend/services/tests/test_e2e_deep_real_infra.py
pytest -q backend/services/tests
```

## 修复记录（2026-03-09，实盘启停状态回写）

- 通知中心联动（交易/实盘优先）：
- `real_trading/start|stop` 会按模式真正拉起/停止运行容器或沙箱任务，但仍会异步发布用户通知（`type=strategy`）；
- `TradingEngine` 在 Broker 拒单、执行异常、成交确认时发布通知；
- `ExecutionStreamConsumer` 在 `order_rejected` 与 `order_filled(最终成交)` 时发布通知。
- 发布方式：
  - 统一通过 `backend.shared.notification_publisher.publish_notification_async`；
  - best-effort，失败仅告警，不阻塞交易主流程。

- `real_trading/start` 与 `real_trading/stop` 成功后，新增“非阻塞异步回写”策略生命周期状态：
  - `start` -> best-effort 回写策略为 `live_trading`；
  - `stop` -> best-effort 回写策略为 `repository`。
- 回写实现特性：
  - 不阻塞主接口返回；
  - 轻量重试 + 告警日志；
  - 回写失败不影响启停主流程成功响应。
- 异常口径：
  - 运行态 `error` 不自动降级生命周期，策略仍保持 `live_trading`，由前端基于 `runtime_state=error` 展示异常。
