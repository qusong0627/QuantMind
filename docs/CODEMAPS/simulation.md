# CODEMAP: simulation（模拟盘）

> 用途：AI/人排障入口——先读本页再定位，目标「直查目标文件 ≤3 跳」。
> 更新纪律：本模块结构变化时同步更新；常见故障链到 `docs/RUNBOOK.md`。

## 职责
模拟账户/撮合/调仓/账本/快照的全链路；信号驱动的托管调仓与手动/内部单执行。

## 入口文件（各一句话）
| 文件 | 职责 |
|---|---|
| `routers/simulation.py` | REST：账户/设置/重置/快照/OCR 同步（uid 经 `require_sim_user_id` 归一） |
| `engine.py` | `SimulationEngine.run_cycle`：信号→调仓→撮合→镜像→快照 |
| `scheduler.py` / `services/simulation_hosted_scheduler.py` | 每日调度（**scheduler.py 曾死代码**，托管走 hosted scheduler，30s 扫描 `trade:active_strategy:*`） |
| `services/rebalance_calculator.py` | TopK/Dropout/权重→目标持股（含可交易性过滤） |
| `services/execution_engine.py` | 成交判定/取价链/费用/落账（`apply_filled`）/拒单 |
| `services/ashare_matcher.py` + `market_rules.py` + `local_market_data.py` | A 股撮合规则 / 五市场规则 / 行情读取 |
| `services/order_submission_service.py` + `pending_order_worker.py` | 手动/内部单统一提交链（锁+幂等）/ 挂单消费 |
| `services/equity_settlement_worker.py` | 30s 周期：对账→行情重估→`simulation_fund_snapshots` 持久化 |
| `services/eod_service.py` | 日终（03:05）：PG 台账投影→`simulation_account_daily`（**依赖 PG 台账非空**） |
| `services/fund_snapshot_service.py` | 用户级快照聚合（跨市场合并；initial 按市场求和 `resolve_account_seed`） |
| `services/ledger_service.py` + `projection_service.py` | PG 台账（唯一事实源方向）/ 投影 |
| `replay/` | 时光回放（day_runner / signal_generator / router） |

## 对外契约
- **Signal**：读 `engine_signal_scores`（`signal_loader.load_latest_signals`，经 `universe_tag` 分市场）
- **Order/Fill**：写 `sim_orders`/`sim_trades`（user_id 为 int 口径）
- **Ledger**：PG `simulation_accounts/lots/cash_ledger` + Redis 热缓存（`simulation:account:{tenant}:{user}[:MARKET]`）

## 依赖
Redis db2（账户/调度锁）· PG（台账/快照）· QuantDB 行情（CN 不复权，其余 forward）· 远端行情 Redis（L0 取价，见 `shared/remote_quote_config.py`）

## 数据流（一图）
```
engine_signal_scores → signal_loader → RebalanceCalculator → _execute_order
  → execution_engine(取价/规则/fees) → apply_filled(Redis Lua + PG ledger + mirror)
  → equity_settlement_worker(30s 重估+快照) / eod_service(日终日级快照)
```

## 常见故障 top5（症状 → 根因 → 文件）
| 症状 | 先跑 | 常见根因 |
|---|---|---|
| 模拟盘不成交 | `python backend/scripts/diagnose/health.py` | 信号未就绪 / 账户键双形 / 风控禁买 / 行情缺失 |
| 资金曲线不对 | health C03/C04 | 用户 ID 口径 / 跨市场种子 / 快照聚合 |
| 日级快照无产出 | health C05 + EOD 日志 | PG 台账为空（EOD 静默空转，T-P1-04） |
| 挂单悬空 | health + `pending_order_worker` 日志 | worker 未注册 / 撮合拒绝 |
| 港股静默 0 单 | hosted scheduler 日志 | market hint 丢失（R2） |

## 禁区（评审红灯项）
- 不许绕过 `order_submission_service`/引擎直接改 Redis 账户；
- 不许手写账户键（必须走 `shared/simulation_account_keys.py`）；
- 不许新增第三套撮合入口（统一收往 OrderRouter 方向）；
- 单测/脚本改 Redis 账户后必须还原或用独立键空间。