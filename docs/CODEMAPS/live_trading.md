# CODEMAP: live_trading（执行 / 镜像 / 实盘）

> 用途：「下单去哪了、为什么没下单、实盘镜像怎么走」的定位地图。

## 职责
策略/信号的**执行侧**：手动与托管任务、内部单分发、真假单镜像、TDX 推送与滚动交易、预检与风控扫描、沙箱策略执行。

## 入口文件
| 文件 | 职责 |
|---|---|
| `routers/real_trading_lifecycle.py` | `POST /real-trading/start|stop`：策略解析→沙箱提交（**经安全闸门**）→active 键→bootstrap 首轮 |
| `routers/real_trading_preflight.py` | 交易前预检（行情/模型/沙箱/快照/远端行情连通等项） |
| `routers/real_trading_utils.py` | 账户快照/远端行情客户端等工具（远端行情配置走 `shared/remote_quote_config.py`） |
| `services/internal_strategy_dispatcher.py` | 内部单统一分发：SIM/SHADOW→模拟提交链；REAL→TradingEngine |
| `services/real_mirror_service.py` | **模拟→真单镜像**：开关/白名单/2% 偏离闸门/限额/熔断/幂等 |
| `services/manual_execution_service.py` | 手动/托管任务：preview→逐单→等待回报（REAL 专属等待/模拟跳过） |
| `services/tdx_rolling_trade_service.py` / `tdx_signal_push_service.py` | 滚动买卖（阈值上证 MA20 规则）/ Top-N 预警推送 |
| `services/risk_trigger_scanner.py` | 全局风控扫描（触发后模拟盘自动平仓） |
| `services/dual_book_reconciliation_task.py` | 每日 15:10 双轨对账（sim vs 真单 client_order_id 前缀 `mir-`） |
| `trade/sandbox/`（manager/worker/context） | 用户策略子进程执行池（**submit_strategy 经 AST 闸门**；worker 内 exec 用户代码） |
| `trade/runner/`（容器） | 信号事件消费 → `risk_gate.apply` → 内部单 HTTP |

## 对外契约
- **Order**：`orders` 表（user_id 字符串口径 `00000001`，trading_mode=REAL/SIMULATION/SHADOW）
- 内部单：`POST /api/v1/internal/strategy/order`（`INTERNAL_CALL_SECRET` 鉴权）
- Redis：`trade:active_strategy:{tenant}:{user}`（运行态唯一事实源）、镜像跳过/对账键 `mirror:*`

## 依赖
模拟模块（SIM 分支）· QMT 桥（REAL 执行/撤单）· TDX 桥 · Redis db2/镜像 Redis · admin 风控配置

## 数据流
```
触发（托管调度/手动任务/TDX/沙箱信号）
  → dispatch_internal_strategy_order（mode 分派）
     ├ SIM/SHADOW → SimulationOrderSubmissionService.submit_and_fill → (可选) mirror_virtual_fill
     └ REAL       → TradingEngine → QMT 桥 → 回报回收
  → 双轨对账（每日）→ mirror:reconcile 报表
```

## 常见故障 top5
| 症状 | 先跑 | 根因 |
|---|---|---|
| 真单没下发 | 查 `mirror:` 跳过键 + 日志 | 白名单空 / 2% 偏离闸门 / 限额 / kill switch |
| 策略启动失败 | 400 vs 500 区分 | 400=AST 闸门拒绝（代码含危险导入）；500=池满/沙箱异常 |
| 状态显示"运行中"实际已死 | `trade:active_strategy:*` + sandbox 进程 | 运行态只在 Redis，进程崩溃无自愈（恢复走 runtime_restorer） |
| 滚动单方向不对 | `trade:tdx_config:runtime` | 阈值/MA20 规则配置 |
| 双轨对账差异 | `mirror:reconcile:{date}` | 真单回报延迟 / 跳过未登记 |

## 禁区
- 不许绕过 `internal_strategy_dispatcher` 直发订单；
- 镜像开关默认 **false** 且 fail-closed（杀进程=不下发，不是放行）；
- 沙箱执行前必须有 `validate_strategy_code`（T-P0-02，有源断言守护）；
- REAL 路径的凭据/密钥一律不进用户代码容器（T-P0-03）。