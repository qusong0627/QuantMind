# MVP 实施计划 · 后端（勾选即进度）

> 版本：v1（2026-09-15）　配合：`docs/统一交易栈_设计方案.md` §12（MVP 定义）
> **使用方式**：每个任务是完成态最小单元——`代码 + 测试 + 验收全过` 才可勾选 `[x]`。任务 ID 全局唯一，提交信息引用 ID（如 `fix(P0-01): ...`）。
> 状态图例：`[ ]` 未开始　`[~]` 进行中　`[x]` 完成　`[-]` 推迟（须写原因）

---

## 总览看板

| 阶段 | 进度 | 完成定义 |
|---|---|---|
| P0 止血+维护基建 | 3/10 | 安全问题清零；体检脚本可跑；回归进 CI |
| P1 契约化 | 0/6 | 四契约落地；交易台数字可下钻 |
| P2 执行统一 | 0/6 | 回测-模拟一致性 diff=0 |
| P3 策略收敛 | 0/5 | 策略全生命周期 E2E |
| P4 选股收敛+评估 | 0/6 | Scanner 替换旧链；体检九项上线 |
| P5+ | — | 见主文档 §12.2 总表（P5 后进入下个迭代再细化） |

---

## P0 止血 + 维护基建

### T-P0-01 安全：删除登录明文密码日志 ✅
- **内容**：`backend/services/api/user_app/services/auth_service.py` 删除 556-565 的 DEBUG 日志（含明文密码、用户名探测信息）
- **验收**：登录流程不再输出凭据；日志扫描无 `password=` 明文字样
- **测试**：`backend/tests/test_auth_log_no_credentials.py`（源扫描闸门，2 条）
- **依赖**：无　**设计**：安全红线（诊断报告）
- **证据（2026-09-15）**：单测 2/2 通过；重启容器后实测登录，`docker logs | grep -c "DEBUG credentials"` = 0（此前每 60s 一条）

### T-P0-02 安全：沙箱执行接入 AST 校验
- **内容**：`real_trading_lifecycle` 启动路径对上传/DB 取出的策略代码调用与 `user_strategy_loader.save_strategy` 相同的 `_validate_code`；抽公共函数一处实现
- **验收**：未过校验的策略无法被 `real-trading/start` 执行
- **测试**：安全回归（构造含 `__import__('os')` 的策略被拒）
- **依赖**：无　**设计**：风险控制体系 §六（安全）

### T-P0-03 安全：敏感环境变量与默认口令
- **内容**：① AI-IDE 容器 `_build_runner_environment` 停止透传 `DB_PASSWORD/SECRET_KEY/JWT_SECRET_KEY/INTERNAL_CALL_SECRET/DASHSCOPE_API_KEY`（按需最小集替代）；② `redis_series_quote.py` 移除硬编码默认口令/公网主机，未配置即禁用该级取价并 WARNING
- **验收**：用户代码容器 env 无敏感项；未配置远端 Redis 时报 WARNING 而非静默连公网默认口令
- **测试**：env 白名单单测 + 配置缺失降级单测
- **依赖**：无　**设计**：LLM 方案 §四 / L2 方案

### T-P0-04 身份归一：曲线端点使用归一 ID ✅
- **内容**：`routers/simulation.py::list_simulation_fund_snapshots` 改 `str(_require_user_id(...))`；全库排查同类散点（`grep` 原始 sub 直读）
- **验收**：admin 的资金曲线读到真实账户（有仓位、有盈亏），与 `/account` 数字一致
- **测试**：一致性回归（同用户 account.total_pnl ↔ 快照行 pnl 同源断言）
- **依赖**：无　**设计**：主文档铁律四
- **证据（2026-09-15）**：live 验证——admin JWT 调 `/simulation/snapshots/daily` 返回 total_asset=2,008,379.58（真实账户），修复前为另一空账户的 1,000,000 平线

### T-P0-05 快照修正：初始资金按市场求和 ✅
- **内容**：① `simulation_manager.init_account` 账户 JSON 落 `initial_cash` 字段；② `fund_snapshot_service` 新增纯函数 `resolve_account_seed(account, market, settings)`（顺序：账户内 initial_cash → 未交易启发式(无持仓且现金≈总资产) → CN 的 settings → None）并按市场求和进 `initial_capital`；③ 未知种子 WARNING 点名
- **验收**：CN+FUTURES 双账户用户快照 total_pnl 不再虚增 +100 万；未交易空账户 pnl=0
- **测试**：`test_fund_snapshot_multimarket.py` 扩至 8 条（多市场求和/未交易启发式/显式种子优先/未知种子降级/零持仓量边界）
- **依赖**：无　**设计**：复盘报告 风险①
- **证据（2026-09-15）**：单测 8/8 通过；重启后 live 快照 user=1：initial_capital 200 万、total_pnl **+8,379.58**（修复前 +1,008,379.58）
- **遗留（已转 P1）**：新市场账户**首日**的 today_pnl 仍含一次性资本注入（今日 101 万）——基线为旧口径 CN-only，明日自愈；正确修法见新增 T-P1-07

### T-P0-06 调度注册：EOD worker + pending order worker
- **内容**：`trade/main.py` 注册 `run_simulation_eod_worker`、`run_simulation_pending_order_worker`（含开关与启动日志）；启动自检断言（关键 worker 未注册即告警）
- **验收**：`simulation_account_daily` 有写入；挂单不再永久 pending
- **测试**：启动注册断言单测 + 一日 E2E
- **依赖**：无　**设计**：主文档 §九

### T-P0-07 一键体检脚本 + 12 项断言
- **内容**：`backend/scripts/diagnose/health.py`（--all/--item/--json）；12 项断言按可维护性文档 §四实现；输出红黄绿 + 文件行号 + 建议
- **验收**：本机一键跑出报告；故意注入 2 类故障（信号全 HOLD / 台账空）能被检出
- **测试**：断言函数单测（注入构造数据）
- **依赖**：T-P0-05（体检含快照一致性项）　**设计**：可维护性 §四

### T-P0-08 错误自定位改造
- **内容**：信号/下单/落账三条路径错误信息统一格式 `[CONTRACT|RULE:ID] 描述 (run_id/order_id) → file:func`
- **验收**：三类真实错误各抽一条，日志过 `grep "→"` 可直接定位文件
- **测试**：格式单测（异常构造）
- **依赖**：无　**设计**：可维护性 §七

### T-P0-09 代码地图 v1
- **内容**：`docs/CODEMAPS/{simulation,inference,live_trading,strategy}.md`，按可维护性文档模板
- **验收**：按图直查目标文件 ≤3 跳（抽测 3 个问题）
- **测试**：无（文档）；抽测记录留档
- **依赖**：无

### T-P0-10 回归测试批（12 条）
- **内容**：按可维护性文档 §六的 12 bug→测试映射，逐条落地；不适用单测的（如 EOD 未注册）用启动断言/脚本断言替代
- **验收**：12 条全绿进 CI；`pytest -m regression` 可单跑
- **依赖**：T-P0-01~06 的代码改动　**设计**：可维护性 §六

---

## P1 契约化（2-3 周）

### T-P1-01 Signal 契约增列
`engine_signal_scores` 加 `market/score_rank/score_pct/source/ts`（迁移脚本可回滚，rank 回填按 run 分组截面）；下游读点全改为 rank 口径。测试：增列回归 + 回填校验（抽样 3 天对账）。

### T-P1-02 就绪标记与单实例锁
`qm:signal:ready:{market}:{date}` 全量校验后才置位；推理任务加分布式锁（修同日多 run 竞态）。测试：并发双跑只有一个执行；残 run 不置位。

### T-P1-03 Order/Fill 契约
新增 order 表字段/视图（mode/price_source/reason/client_order_id 语义统一）；三环境写入点对齐。

### T-P1-04 Ledger 契约
`sim_orders/sim_trades/ledger` 写入闭环断言（成交必落账）；Redis 重建按市场（修跨市场串账）。测试：成交后台账非空断言 + 重建不串市场。

### T-P1-07 资本注入调整 + 快照市场维度（P0-05 遗留）
新市场账户**首日**的 `today_pnl` 不能把种子算成当日盈利：① 快照表加 `market` 列（前端注释里的"方案 B2"，解决非 CN 面板无市场维度）；② `get_baselines` 按市场对齐日初基线，或按"新账户出现的当日将种子计入基线"。测试：新建市场账户当日 today_pnl ≈ 0。

### T-P1-05 今日交易台后端聚合 API
`/api/v1/desk/today`：数据✓推理✓→候选→计划→执行→盈亏 + 健康卡（与体检脚本同源）；所有数字带 `source` 下钻字段。

### T-P1-06 调度表统一
散落 worker/beat 收敛为一条注册表（含开关、心跳、手动重跑 `--date --force`）；心跳进体检。

---

## P2 执行统一（3-4 周）

### T-P2-01 OrderRouter（唯一入口）
五条下单路径收敛；账户锁 + 幂等 + 状态机；沙箱/镜像改走 Router。

### T-P2-02 撮合规则单实现
matcher/market_rules 提升为全模式共用；回测接入同一实现。

### T-P2-03 取价契约
`price_mode=auto` + `price_source` 标注 + 陈旧价守卫（托管路径补齐）；修"昨收成交"。

### T-P2-04 退出规则单一实现
策略 risk / 风控 / QMT sltp 三处收敛为一处（主文档 P2 项）。

### T-P2-05 回测-模拟一致性测试
同策略同区间：回测订单序列 vs 模拟重放 diff=0（进 CI）。

### T-P2-06 模拟盘成交即落账 + 影子对照
成交落账闭环 + shadow 对照报告（模拟-实盘偏差指标）。

---

## P3 策略收敛（2-3 周）

- **T-P3-01** 策略注册表状态机（DRAFT→VERIFIED→SIM→LIVE）+ 版本 + 参数锁
- **T-P3-02** `strategy_storage.delete` async 修复 + E2E
- **T-P3-03** 旧 5 格式 → 2 格式转换器/下架清单
- **T-P3-04** AI-IDE 接入统一回测（结果落库 + 触发 verified）
- **T-P3-05** 晋级门槛总表（唯一事实源，模型/策略/环境三方引用）

## P4 选股收敛 + 评估（2 周）

- **T-P4-01** Scanner SPI + 模型信号扫描器迁入（等价性验证）
- **T-P4-02** 旧 `/selection` 三层过滤退休（声明 + 引导到新入口，Claude skills 同步改）
- **T-P4-03** 阈值分位化全量替换 + 量纲回归测试
- **T-P4-04** 买入前 K 线过滤（移植 KHunter 4 规则）
- **T-P4-05** 五张评分卡 + 回测体检九项（`scripts/eval/` + `eval_scores` 表）
- **T-P4-06** 体检三处接入（回测后/晋级门禁/月度复检）

---

## 全局 DoD（每个任务）

1. 代码 + 单测（或断言脚本）+ 验收证据（命令与输出）三者齐全；
2. 涉及契约的改动跑契约测试；涉及口径的跑一致性测试；
3. 完成即勾选本文件，写一行"证据摘要"（命令/结果）；
4. 提交信息带任务 ID。