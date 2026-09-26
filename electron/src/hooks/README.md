# hooks

用途：React Hooks 与复用逻辑。

## 说明
- 归属路径：electron\src\hooks
- 修改本目录代码后请同步更新本 README

## 刷新机制（2026-02 更新）
- 仪表盘核心模块（资金/交易/策略/图表/通知）不再各自使用高频 `setInterval` 轮询。
- 模块刷新统一注册到 `refreshOrchestrator`（`electron/src/services/refreshOrchestrator.ts`）触发。
- Hook 内部新增数据指纹比对（`electron/src/utils/dataChange.ts`）：
  - 有变化才 `setState`
  - 无变化跳过渲染，减少闪烁和不必要重绘
- 手动刷新仍保留，但默认采用静默刷新策略。

## useNotifications（2026-03 更新）
- 通知中心改为“首屏 HTTP + WebSocket 增量 + 低频轮询兜底”：
  - 首屏读取 `/api/v1/notifications`
  - 实时订阅 `notification.{user_id}`
  - WS 断开时自动降级为轮询刷新
- 返回值新增：
  - `total`：通知总数
  - `degraded`：通知接口当前是否处于降级状态
  - `realtimeStatus`：`connected | fallback | disabled`
  - `connectRealtime / disconnectRealtime`
- 数据口径统一为后端通知域字段，不再依赖历史 `message/priority/updated_at` 别名。
- 新增滚动续拉能力（2026-03-13）：
  - 返回值新增 `loadedCount / hasMore / loadingMore / loadMore`。
  - 支持基于 `limit + offset` 逐页加载历史通知（用于展开态滚动到底自动续拉）。
  - `total` 保持后端全量语义，前端可明确展示“已加载 X / 总数 Y”。
- 新增清除视图能力（2026-03-13）：
  - 返回值新增 `clearNotifications()`。
  - `clearNotifications()` 会调用后端 `POST /api/v1/notifications/clear`（默认按当前 `days` 窗口清除）。
  - 清除成功后前端即时清空并通过协调器刷新，保持与服务端状态一致。

## useTradeRecords（2026-02 补充）
- 返回值新增：
  - `isStale`：交易服务延迟或短时离线，但仍保留上次成功数据。
  - `lastUpdatedAt`：最近一次成功拉取时间（ISO 字符串）。
- 数据处理策略：
  - 对返回记录按 `id` 去重。
  - 按时间倒序（最新一条在最上方）。
  - 截断为固定 `limit`（默认 10 条）。
- 异常与重连策略：
  - 网络异常时不强制清空已有交易记录，避免界面闪空。
  - 使用指数退避自动重试（起始 3s，上限 30s）。
- 成交优先与模式联动（2026-03-09 更新）：
  - `useTradeRecords` 默认消费“成交优先”服务层结果（成交接口失败时自动降级委托）；
  - 返回值新增 `isFallbackToOrders`，用于前端提示“委托降级视图”；
  - 支持透传 `tradingMode`（`real|simulation`），按当前模式过滤数据，避免混单。

## useTradeWebSocket（2026-04 更新）
- 实盘页会在挂载时主动调用 `websocketService.connect()`，避免上层 Provider 未自动连上时丢失实时订阅。
- 订阅主题固定为 `trade.updates.{userId}`，收到 `TRADE_UPDATE` 后统一触发页面级刷新。
- 监听器清理已改为稳定函数引用，避免页面重复进入/退出后残留旧监听导致重复刷新。
- 当前仍保留轮询兜底，因此实时推送失效时页面不会完全失去更新能力。
- 全局 WebSocket 服务已支持“首次鉴权未就绪”的补连流程，因此首次进入页面若 token 还在加载中，也会自动恢复到已连接状态。

## useTradingModeInitialization（2026-04 更新）
- 实盘入口已在前端移除，该 Hook 现在把交易模式**强制为 simulation**（`uiSlice` 的初始值同样是 `simulation`），不再读取历史遗留的 `qm:trading_mode_pref` 偏好。
- 页面侧若需要决定读取哪个账户来源，应通过统一适配层 `pages/trading/utils/accountAdapter.ts` 与 `realTradingService.getRuntimeAccount()` 完成，不要在 Hook 中隐式改写全局模式。

## useStrategies（2026-02 补充）
- 响应判定兼容 `code` 与 `success` 两种成功语义，降低前后端响应风格差异带来的解析失败风险。
- 运行态兜底联动（2026-03-20）：
  - 当策略列表无 `running/starting`，但 `/api/v1/real-trading/status` 明确返回运行中时，Hook 会按 `strategy.id / strategy.name / parameters.strategy_type(sys_模板ID)` 回填运行态；
  - 修复“实盘控制台显示运行中，但仪表盘策略卡全部已停止”的短期口径不一致问题。
- 启停策略改为真实调用服务层 `startStrategy/stopStrategy`，保留乐观更新以保证交互即时反馈。
- 启停失败时执行状态回滚并写入错误信息，避免 UI 状态与后端实际状态长期不一致。
- 返回值新增：
  - `isStale`：接口异常时是否处于“使用旧数据”的延迟状态。
  - `lastUpdatedAt`：最近一次成功获取策略数据的时间。
- 状态统计口径（2026-03-09 更新）：
  - `activeStrategies` 计数包含 `running + starting`；
  - 与策略监控卡 `effective_status` 展示口径一致。
- 乐观更新口径（2026-03-09 更新）：
  - 启动策略时临时状态改为 `starting`；
  - 停止策略时临时状态改为 `stopped`；
  - 成功后仍会以服务端返回状态覆盖本地临时态。

## useIntelligenceCharts（2026-02 补充）
- 返回值新增：
  - `isStale`：图表数据是否处于延迟状态（请求失败但已有旧数据）。
  - `lastUpdatedAt`：最近一次成功拉取图表数据时间。
  - `hasDailyReturn / hasTradeCount / hasPositionRatio`：分区图表是否有可渲染数据。
- 设计目标：
  - 支持模块“固定骨架 + 动态填充”渲染。
  - 无数据时保持坐标轴和图槽结构稳定，避免布局闪烁。
- 开发阶段调试开关：
  - 默认开启自动拉取智能图表数据接口（会触发 `portfolios/*` 与 `trades/stats/summary` 请求）。
  - 通过 `VITE_INTELLIGENCE_CHARTS_AUTO_FETCH=false` 可显式关闭自动拉取与刷新注册。
  - 默认行为用于保障“开页即读库出图”；仅在排障或后端维护窗口时建议临时关闭。
- 持仓分布解析增强（2026-03-20）：
  - `positionRatio` 兼容解析 `data.data.sectors`、`data.sectors`、`sectors`、`assets` 以及 `positions[]` 多种返回结构；
  - 当 `sectors` 为空时会自动回退 `assets`，避免仪表盘误显示“暂无持仓分布”。
  - 展示口径简化为固定两类：`持仓` 与 `空仓`，不再按行业/个股拆分切片。
  - 当分布接口返回空时，自动回退读取实盘账户快照（`market_value/cash/total_asset`）计算“持仓/空仓”占比。
- `current` 用户解析修复（2026-04-06）：
  - 当 `useIntelligenceCharts('current')` 被调用时，Hook 会优先解析登录态中的 `user_id/id`，避免请求参数长期携带占位值 `user_id=current`。
  - 若尚无可用用户标识，会跳过本轮图表接口请求并维持空态渲染，不再发起无效请求。
- 账本优先（2026-04-09）：
  - 每日收益图优先读取实盘账户日账本接口 `GET /api/v1/real-trading/account/ledger/daily`；
  - 若当日账本暂未生成，再回退到传统日收益接口，避免 Redis 短时抖动直接影响图表曲线。
- 收益率口径修复（2026-04-11）：
  - `useIntelligenceCharts` 的首图数据源已从“日盈亏金额”切换为“日收益率”；
  - 账本路径优先读取 `daily_return_pct`，实时账户则仅补充 `daily_return`，不再把 `today_pnl` 金额混入同一条时间序列。
- 收益率字段兼容增强（2026-04-13）：
  - 当账本字段 `daily_return_pct` 缺失或为空时，Hook 会自动回退 `daily_return_ratio * 100`，再回退 `daily_return`；
  - 兼容旧环境字段口径差异，避免“智能图表-每日收益率”出现空白。
- 交易日历对齐（2026-04-11）：
  - 智能图表已复用模型推理同源的交易日历接口（`/api/v1/market-calendar/*`）补齐最近 30 个交易日与最近 7 个交易日窗口；
  - 每日收益与交易次数横轴均按交易日历序列渲染，不再按自然周/自然日补点。

## useFundData（2026-03 更新）
- `userId/tenantId` 默认解析已改为“登录态优先”（`authService.getStoredUser()` + `localStorage tenant_id`），不再回退固定 `default_user`。
- 目的：避免实盘账户接口出现 `403 Forbidden user_id override`，确保资金概览请求与 JWT 身份一致。
