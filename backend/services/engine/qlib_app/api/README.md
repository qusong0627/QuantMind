# Qlib API

接口路由定义，提供回测与参数优化功能。

## 回测历史策略名修复（2026-03-28）
- `GET /qlib/history/{user_id}` 现在会在返回列表中补充 `strategy_display_name`：
  - 优先根据模板 ID 解析模板中文名；
  - 兼容 `strategy_name / config.strategy_name / config.qlib_strategy_type / strategy_type` 多来源字段。
- 对于历史数据中仍使用模板 ID 的记录，接口会在可识别场景下回填可读名称，避免前端列表显示原始 ID。

## 回测入口与 WebSocket 收口（2026-04-10）
- `api/backtest.py` 的异步入队失败与同步回测异常日志已统一为结构化事件格式；
- `websocket/connection_manager.py` 的连接、断开、广播与失效清理日志已统一为结构化事件格式；
- 任务状态、实时推送与回测入口现在与其它 Qlib 子模块保持一致的排障口径。

## 快速导出数量口径修复（2026-04-22）
- `export_utils._build_quick_trade_rows` 改为“显式 `price/quantity` 优先”：
  - 仅当显式字段缺失时，才回退 `adj_price/adj_quantity/factor` 还原展示；
  - 避免已有展示数量再次被复权重算覆盖，导致导出出现近整手抖动（如 `2700 -> 2702`）。
- A 股导出数量新增整手容差纠偏（容差 `<=2` 股），与回测结果展示保持一致。

## 回测 CSV 导出口径统一（2026-04-02）
- `GET /qlib/export/{backtest_id}/csv` 默认切换为 `quick` 交易流水格式，与“快速回测 > 导出数据”保持一致：
  - 列头：`日期,代码,方向,成交价,成交量,成交金额,手续费,权益余额`
  - 数据源优先使用 `result.trades`（兼容回退 `trade_list`）
  - 权益余额优先匹配 `equity_curve` 同日权益，缺失时回退 `equity_after/balance/运行余额`
- 新增 `style` 查询参数：
  - `style=quick`（默认）：统一口径导出
  - `style=legacy`：旧版“指标 + 交易明细”结构（兼容保留）

## Instruments 口径
- `instruments` 相关接口默认读取 `db/qlib_data/instruments/all.txt`。
- 返回结果统一剔除北交所（`BJ*`）标的。

## 参数优化异步模式
- `GET /qlib/optimization/history`
- `GET /qlib/optimization/{optimization_id}`
- `POST /qlib/optimize?async_mode=true`
- `POST /qlib/optimize/genetic?async_mode=true`
- 网格搜索请求现由后端统一校验参数组合数上限 `<=40`；超过上限时接口直接返回 `422`，避免绕过前端提交超大任务。
- 网格搜索异步任务现按全局单槽位串行执行：同一时间仅允许 1 个参数优化实际运行，后续提交会进入 `pending/queued` 队列等待，不再并发抢占计算资源。

异步模式返回 `optimization_id` 与 `task_id`，通过任务状态接口查询结果。
- `GET /qlib/task/{task_id}/status` 增加任务信息序列化兜底（异常对象/不可序列化结构将被安全转字符串或标准错误对象），避免轮询阶段因 `info` 序列化失败返回 `500`。

## 回测异步模式（Celery-only）
- `POST /qlib/backtest?async_mode=true` 仅支持 Celery 入队执行。
- 当 Celery 不可用时，接口返回 `503`，不再回退到 API 进程内 `BackgroundTasks` 执行。
- `run_backtest` 对 `HTTPException` 直接透传，避免 `503` 被二次包装成 `500`。
- `POST /qlib/task/{task_id}/stop` 现作为通用任务停止入口，可撤销回测/优化类 Celery 任务，并按任务归属更新持久化状态为 `cancelled`。

## Redis 连接口径（health/logs）
- `GET /qlib/health` 与 `GET /qlib/logs/{backtest_id}` 统一读取：
  - `REDIS_HOST`
  - `REDIS_PORT`
  - `REDIS_PASSWORD`
  - `REDIS_DB`（未配置时回退 `REDIS_DB_DEFAULT`）
- 目的：避免开启 Redis 认证后误报 `redis_ok=false` 或日志接口 `Authentication required`。
- `GET /qlib/logs/{backtest_id}` 仅基于认证租户读取 Redis 日志键，不再依赖回测状态预检查；
  - 同时支持回测任务与参数优化任务日志读取；
  - `start_index` 增加 `ge=0` 校验；
  - Redis 不可用时返回 `503`（不再吞异常返回 `200`）。

## WebSocket 鉴权（2026-03-08）
- `GET /api/v1/ws/backtest/{backtest_id}` 与 `GET /api/v1/ws/risk-monitor/{backtest_id}` 现要求携带有效 token（query `token` 或 `Authorization: Bearer`）。
- 服务端会校验 token 身份与 `backtest_id` 归属（`user_id + tenant_id`），不满足即拒绝连接（1008）。
- 若必须兼容历史无 token 调用，可在引擎环境中设置 `QLIB_WS_ALLOW_QUERY_IDENTITY=true`（仅回测 WS，默认关闭）。

## 历史接口兼容性修复
- `GET /qlib/history/{user_id}` 过滤与排序逻辑兼容 `dict` 与 `QlibBacktestResult` 两种返回项。
- 解决历史列表查询时 `QlibBacktestResult object has no attribute get` 导致的 500。
- `GET /qlib/history/{user_id}` 默认排除参数优化产生的子回测（`config.history_source=optimization`），避免“快速回测历史”被污染。
- 针对历史遗留数据（缺少 `history_source` 标记），接口会额外按 `qlib_optimization_runs.all_results_json` 反查 `backtest_id` 并过滤。
- 针对更早期且无法反查 `optimization_runs` 的遗留数据，接口增加“5分钟窗口多参数组合聚类”兜底过滤（同用户/租户/区间在同一时间窗口出现大量不同 `topk/n_drop` 组合时视为优化子回测）。
- 如需查看全部历史（含优化子回测），可传 `include_optimization=true`。
- `POST /qlib/task/{task_id}/stop` 先将关联优化任务状态置为 `cancelled`，再发送强终止信号，减少“前端已停止但后台继续写进度/历史”的窗口。

## 策略列表聚合修复（2026-03-19）

- `GET /api/v1/strategies` 现在会把最近一次回测摘要按 `config_json.strategy_id` 回填到策略列表中，用于前端“策略监控”卡片展示。
- 允许前端直接从策略列表获取：
  - `total_return`
  - `today_return`
  - `risk_level`
  - `last_update`
  - `error_message / error_code`
- 当前运行态口径仍以 `running / starting / stopped / error` 为准，避免把后端不存在的“暂停”状态当作独立监控维度。

## 身份来源约束（2026-02-25）
- `qlib` 读写接口统一从 `request.state.user` 获取 `user_id/tenant_id`。
- Query/Body 中携带的 `user_id`/`tenant_id` 仅用于防伪校验，不再作为实际查询条件。
- 当 Query/Body 身份与认证身份不一致时，接口返回 `403`。

## AI 修复提示词对齐（2026-03-09）
- `POST /qlib/ai-fix` 诊断提示词已同步 QuantMind Qlib 规范 V1.1：
  - 新增 `reset(*args, **kwargs)` 兼容要求；
  - 明确在 `level_infra/common_infra/trade_exchange` 参数差异下的回退写法；
  - 避免 AI 修复后再次出现 `reset() got an unexpected keyword argument 'level_infra'`。
