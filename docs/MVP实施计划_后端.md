# MVP 实施计划 · 后端（勾选即进度）

> 版本：v1（2026-09-15）　配合：`docs/统一交易栈_设计方案.md` §12（MVP 定义）
> **使用方式**：每个任务是完成态最小单元——`代码 + 测试 + 验收全过` 才可勾选 `[x]`。任务 ID 全局唯一，提交信息引用 ID（如 `fix(P0-01): ...`）。
> 状态图例：`[ ]` 未开始　`[~]` 进行中　`[x]` 完成　`[-]` 推迟（须写原因）

---

## 总览看板

| 阶段 | 进度 | 完成定义 |
|---|---|---|
| P0 止血+维护基建 | 10/10 ✅ | 安全问题清零；体检脚本可跑；回归进 CI |
| P1 契约化 | 6/6 ✅ | 四契约落地；交易台数字可下钻 |
| P2 执行统一 | 7/7 ✅ | 回测-模拟一致性 diff=0（执行层，含真实数据）；盘后固定价格窗口；成交落账闭环+影子对照 |
| P3 策略收敛 | 4/7（+T-P3-07 市场时段参数化+美股模板） | 策略全生命周期 E2E |
| P4 选股收敛+评估 | 0/6 | Scanner 替换旧链；体检九项上线 |
| P5+ | — | 见主文档 §12.2 总表（P5 后进入下个迭代再细化） |

---

## P0 止血 + 维护基建

> **批次二实施细案（2026-09-15，先规划后写）**
>
> **T-P0-02 沙箱 AST 校验（唯一卡点设计）**
> - 单一实现：复用 Strategy Lab 的 `ast_checker.check_source`（白名单+禁用内建+禁用属性），不新写校验器；
>   新增薄包装 `backend/shared/strategy_code_gate.py::validate_strategy_code()`（平台口径：`allowed_modules = ALLOWED_MODULES ∪ {backend, minibt}`，`require_hooks=False` —— 沙箱代码有 on_tick/STRATEGY_CONFIG/minibt 多种形态，不强求 Lab 的 setup 钩子）。
> - **唯一必经卡点**：`trade/sandbox/manager.py::submit_strategy()` 入口处校验（所有调用方都经过它，一处理全覆盖）；`user_strategy_loader.save_strategy` 的弱黑名单 `_validate_code` 一并替换为共享闸门（保存时早期反馈）。
> - 兼容性前置检查（先跑再签收）：脚本扫 `strategy_templates/*.py` 全部模板 + DB `strategies.code` 存量，过闸门；**误杀必须为空**，有则逐个豁免并记录理由。
> - 测试：`test_strategy_code_gate.py` —— os/subprocess/`__import__`/`eval`/`open`/`__class__` 六类各一例被拒；合法模板通过；钩子非必需语义。
>
> **T-P0-03 敏感 env 与远端口令**
> - `ai_ide/executor.py` 停止透传 5 个密钥类变量：`SECRET_KEY / JWT_SECRET_KEY / INTERNAL_CALL_SECRET / DASHSCOPE_API_KEY / QWEN_API_KEY`（runner 只跑回测，不需要签钥与 LLM Key；DB/REDIS 保留，runner 需要数据访问）。
> - **P0-03 后续跟踪（记入 P1）**：runner 专用只读 DB 账号（现在 user 代码容器可用全权 DB 凭据，属遗留风险面）。
> - `redis_series_quote.py`：删除硬编码默认（公网主机 + `quantmind2026` 口令）；未配置 `REMOTE_QUOTE_REDIS_HOST` 即**禁用该级取价**（返回 None 走兜底），首次访问 WARNING 一次。
> - 测试：env 白名单断言（source 扫描 5 键不在 passthrough）；`_env()` 缺失/完备两态单测。
>
> **T-P0-06 调度注册（EOD + 挂单）**
> - `trade/main.py` 按现有模式注册两个 worker：`run_simulation_eod_worker`（开关 `SIM_EOD_WORKER_ENABLED` 默认 true）、`run_simulation_pending_order_worker`（开关 `SIM_PENDING_ORDER_WORKER_ENABLED` 默认 true），各自 `app.state` 持引用 + 启动日志。
> - 新增 `app.state.registered_workers` 汇总列表 + 启动后一行日志列出关键 worker（为 T-P0-07 体检的"调度心跳"断言提供口径）。
> - 测试：源扫描 tripwire（main.py 必须出现两个 worker 注册）——防未来重构误删。
>
> 回滚口径：三个任务均为增量（新文件/新增分支），回滚 = 还原对应文件；T-P0-02 闸门如出现误杀，豁免名单在 `strategy_code_gate.py` 内集中维护。
>
> **批次三实施细案（2026-09-15，先规划后写）**
>
> **T-P0-07 体检脚本**：`backend/scripts/diagnose/health.py`，10 项断言（原 12 项中「模型契约一致」「风控状态」暂缓——前置设施未就绪，待 P1/P4 补）。
> 结构：`CheckResult(id/name/level/detail/suggestion/metrics)` + `HealthContext(redis/session_factory/today)` 注入式设计；
> 判定逻辑抽纯函数（如 `classify_signal_distribution`）供单测，IO 只做取数。CLI：默认全跑 / `--only C01,C03` / `--json`；有 fail 退出码=1（可直接进 cron/CI）。
> 十项：C01 信号分布（全 HOLD/坍缩）· C02 信号就绪标记 · C03 账户键一致性（1 vs 00000001 类）· C04 快照↔Redis 同源 · C05 台账写入（成交必落账，当前红→T-P1-04）· C06 对账差异 · C07 调度心跳（推理完成标记+收盘核对）· C08 数据同步新鲜度 · C09 远端行情配置状态 · C10 账户种子存在性（T-P0-05 配套）。
>
> **T-P0-08 错误自定位**：新增 `backend/shared/errfmt.py::locate(tag, msg, ref=, where=)`（`[CONTRACT:XX]/[RULE:XX] … (ref=…) → file:func` 三段式）；
> 落三条关键路径：信号闸门（script_runner 零 BUY+SELL 告警）、下单拒绝（order_submission_service / pending_order_worker）、落账异常（ledger_service / execution_engine.apply_filled）。
>
> **T-P0-09 代码地图**：`docs/CODEMAPS/{simulation,inference,live_trading,strategy}.md`（模板：职责/入口/契约/依赖/数据流/常见故障 top5/禁区）；验收=按图抽测 3 个问题直查 ≤3 跳。
>
> **T-P0-10 回归批（诚实口径）**：12 条中 **6 条随修复落地**（#3 昨收成交→P2、#4 量纲→P4、#5 池不符→P4、#6 min_score→P4、#7 delete→P3、#12 side→P2/P4）——不为尚不存在的行为预写测试；**本批新增可达的 2 条**（#2 曲线身份源断言、#9 台账检测的体检单测），加上已完成的 4 条（#1/#8/#10/#11）= P0 期间共 6 条 + 映射表标注余项落地时机。

### T-P0-01 安全：删除登录明文密码日志 ✅
- **内容**：`backend/services/api/user_app/services/auth_service.py` 删除 556-565 的 DEBUG 日志（含明文密码、用户名探测信息）
- **验收**：登录流程不再输出凭据；日志扫描无 `password=` 明文字样
- **测试**：`backend/tests/test_auth_log_no_credentials.py`（源扫描闸门，2 条）
- **依赖**：无　**设计**：安全红线（诊断报告）
- **证据（2026-09-15）**：单测 2/2 通过；重启容器后实测登录，`docker logs | grep -c "DEBUG credentials"` = 0（此前每 60s 一条）

### T-P0-02 安全：沙箱执行接入 AST 校验 ✅
- **内容**：唯一实现复用 Strategy Lab `ast_checker`；新增 `backend/shared/strategy_code_gate.py`（平台口径：放行 backend/minibt，不强制 setup 钩子）；**唯一卡点 = `sandbox/manager.py::submit_strategy`**（启动 + 重启恢复两条真实调用方全覆盖）；`user_strategy_loader._validate_code` 弱黑名单替换为共享闸门；`real-trading/start` 校验失败返回 **400**（含 issue 明细）而非 500
- **验收**：未过校验的策略无法被执行；存量代码零误杀
- **测试**：`test_strategy_code_gate.py`（8 条：os/subprocess/dunder/eval/exec/open/`__import__` 全被拒 + 合法模板/on_tick 通过 + 空/超限/语法错）+ 卡点接线源断言
- **证据（2026-09-15）**：**兼容性前置检查 87 模板 + 78 存量策略 0 误杀**；单测全绿

### T-P0-03 安全：敏感环境变量与远端行情配置 ✅
- **内容**：① AI-IDE runner 停止透传 `SECRET_KEY/JWT_SECRET_KEY/INTERNAL_CALL_SECRET/DASHSCOPE_API_KEY/QWEN_API_KEY`（DB/REDIS 保留）；② 远端行情 Redis 默认值（公共免费行情服）**收敛到 `backend/shared/remote_quote_config.py`**（此前 redis_series_quote 与 real_trading_utils 各写一份），新增 `REMOTE_QUOTE_DISABLED` 总开关；compose 增透传 + .env.example 补文档
- **验收**：docker inspect runner 环境无 5 个密钥；远端行情两处消费共用同一配置源
- **测试**：`test_remote_quote_config.py`（默认免费源/覆盖/禁用/根 .env 兜底 + executor 白名单源扫描）
- **遗留跟踪（P1）**：runner 专用只读 DB 账号（现 runner 仍持全权 DB 凭据）

### T-P0-06 调度注册：EOD worker + pending order worker ✅（端到端待 T-P1-04）
- **内容**：`trade/main.py` 注册两个 worker（开关 `SIM_EOD_WORKER_ENABLED`/`SIM_PENDING_ORDER_WORKER_ENABLED` 默认 true）+ `app.state` 引用 + 启动日志
- **证据（2026-09-15）**：重启后日志 "Simulation EOD worker started"(trigger 03:05) + "Simulation pending order worker started interval=15s"
- **实测发现（转 P1）**：手动触发 EOD 返回 True 但 `simulation_account_daily` 仍 0 行——**EOD 按 PG 台账枚举账户，而台账为空（Ledger 写入链未填充）→ 静默空转**。已加 fail-loud 告警点名；端到端验收挂 **T-P1-04**（台账填充后自动激活）
- **测试**：`test_worker_registration_source.py` 源断言（防重构误删注册）

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

### T-P0-07 一键体检脚本 + 12 项断言 ✅
- **内容**：`backend/scripts/diagnose/health.py`，10 项断言（原 12 项中「模型契约一致」「风控状态」待 P1/P4 前置设施）；判定逻辑纯函数化 + HealthContext 注入式（query/redis_get/redis_scan）；`--only/--json`；有 fail 退出码 1
- **验收**：一键跑出报告；注入 2 类故障可检出
- **测试**：`test_health_checks.py` 14 条（纯判定 + 假上下文驱动 C03/C04/C05）
- **证据（2026-09-15）**：容器实测输出 8 ok/1 warn/1 fail——**C03 当场抓到本机双键形（1 vs 00000001）**，符合真实故障认知
- **依赖**：T-P0-05

### T-P0-08 错误自定位改造 ✅
- **内容**：`backend/shared/errfmt.py::locate`（`[CONTRACT|RULE:ID] 描述 (ref=…) → file:func` 三段式）；落四处：信号闸门零 BUY/SELL 告警、模拟单拒单（新增可见告警）、落账失败、提交失败
- **验收**：三类真实错误过 `grep "→"` 可直接定位
- **测试**：`test_errfmt.py` 3 条（格式 + 三路径源断言防误删）
- **证据（2026-09-15）**：单测全绿；report 中带 `[RULE:SIGNAL-GATE] … → script_runner.py:_resolve_signal_sides` 形态

### T-P0-09 代码地图 v1 ✅
- **内容**：`docs/CODEMAPS/{simulation,inference,live_trading,strategy}.md`（职责/入口/契约/依赖/数据流/故障 top5/禁区）
- **验收**：抽测 3 问直查 ≤3 跳——「模拟盘不成交先跑什么」（→ health.py）✓、「沙箱在哪里校验」（→ live_trading 禁区+manager.py）✓、「策略为什么删不掉」（→ strategy 故障表首行）✓，全部 **1 跳**

### T-P0-10 回归测试批（12 条 → 6 条 P0 落地 + 6 条随修复） ✅
- **内容**：映射表更新落"落地口径"列（可维护性文档 §六）；本批新增 #2 曲线身份源断言
- **证据**：P0 期间落地 6 条（#1/#2/#8/#9/#10/#11），#3/#4/#5/#6/#7/#12 随 P2/P3/P4 修复批次提交（不为尚不存在的行为预写测试）

---

## P1 契约化（2-3 周）

> **批次一实施细案（2026-09-15，先规划后写）**
>
> **T-P1-01 Signal 契约增列（本批）**
> - **命名决策**：既有 `score_rank INTEGER`（名次旧口径，selection/手动任务在消费）**保持不变**；分位以新列
>   `rank_pct DOUBLE PRECISION`（percent_rank 口径 0..1，与 PG `percent_rank()` 对齐）新增。另加
>   `market TEXT`（历史 NULL 视为 CN）、`source TEXT`（batch|realtime，NULL 视为 batch）、`signal_ts TIMESTAMPTZ`。
>   设计文档 §4.1 的 score_rank 表述以此为准修正。
> - **迁移机制**：沿用自愈式先例（`rd_agent_persistence` 的 `ALTER ... ADD COLUMN IF NOT EXISTS`），收敛为
>   `backend/shared/signal_contract.py::ensure_signal_contract_columns(session)`（进程内一次标记）；
>   两个写入端（script_runner 主链 / realtime_contract 实时链）入口自愈；`db_init.sql` 同步（新装）。
> - **分位计算**：`compute_rank_pct(scores)` 纯函数（并列取最小名次、n==1→0.0、非有限值→None），与回填 SQL 的
>   `percent_rank()` 同口径（有测试断言两者一致）。
> - **回填**：`backend/scripts/backfill_rank_pct.py`（默认 dry-run；按 run 分批 UPDATE where rank_pct IS NULL；
>   幂等可重跑；1426 万行/2914 run 全量回填）。
> - 测试：`test_signal_contract.py`——分位口径（并列/单值/NaN/空）+ 写入端 SQL 含新列源断言 + 迁移 SQL 幂等形态。
- **事故记录（2026-09-16，lessons learned）**：自愈迁移最初对热表**无条件** `ADD COLUMN IF NOT EXISTS`——
  PG 即使列已存在也要申请 **AccessExclusive**；当调用方自身事务未提交（Step 0.1 的 DELETE 持 RowExclusive）
  而迁移走独立连接时，**自阻塞 44 分钟**（inference 主会话 idle-in-transaction↔自己的迁移连接互等），
  排队中的 AccessExclusive 进而**堵死整张表的全部读写**（回填/推理/查询集体排队）。
  修复（本批提交）：两契约模块统一改为 **information_schema 预检 → 列齐全零 DDL 快路径 →
  仅缺列才 ALTER（SET LOCAL lock_timeout=3s 快速失败）→ 异常只告警不抛出**；
  现场经 `pg_cancel_backend`（取消无意义 ALTER）秒级疏通，回填恢复全速。
  **教训固化**：① 运行时 DDL 永远先 existence 预检；② 自愈迁移必须 lock_timeout 且失败不阻断业务；
  ③ 自愈测试加 `test_ensure_is_lock_safe`（源码断言三项纪律）。
>
> **T-P1-02 就绪标记与单实例锁（下一批）**：`qm:signal:ready:{market}:{date}`（全量校验后置位）+
> 推理任务分布式锁（修同日多 run 竞态）；先例：`qm:inference:completed:*`（script_runner:143）与
> hosted scheduler 的分布式锁（`qm:hosted:simulation:*`）。

### T-P1-01 Signal 契约增列 ✅
`engine_signal_scores` 加 `market/rank_pct/source/signal_ts`（**已实现并上库**：自愈式迁移 `shared/signal_contract.py`、两写入端接线、`db_init.sql` 同步、设计文档 §4.1 已对齐 rank_pct 口径）；回填脚本 `backend/scripts/backfill_rank_pct.py`（幂等分窗 UPDATE，与 PG percent_rank() 同口径，有对照测试）。下游读点全改为 rank 口径（**待办：下游 rank 化 → T-P4-03**）。测试：`test_signal_contract.py` 11 条（纯函数/PG 对照/写入端源断言/迁移幂等）。
- **回填完成（2026-09-16）**：**14261830/14261830 全填、剩余 0**；25 个分窗逐批计数吻合；跨期抽验（2024-08-05 / 2026-09-15）0 NULL、min/max=0.00/1.00；期间遭遇并修复热表 DDL 自阻塞事故（见事故记录），修复后全速完成。

### T-P1-02 就绪标记与单实例锁 ✅
- **内容**：`backend/shared/inference_lock.py` 唯一实现（SET NX EX 获取 + token + **Lua CAS 释放**）；锁三层收敛：
  `runner.execute` 包装（全市场持久化 run 的唯一咽喉点，覆盖 4 类触发方）、celery 任务级、admin 手动——
  **admin 与 celery 全局任务统一为同 scope 键形**（此前两端键形不同，"防并发"注释与事实不符 = 同日双跑疑因之一）；
  三个本地常量副本全部清除；TTL 1800→3600（旧值小于运行时长会让锁中途过期，反而放行双跑）。
  就绪标记 `qm:signal:ready:{market}:{date}`：非部分推且标的数 ≥ `SIGNAL_READY_MIN_SYMBOLS`(默认 1000) 才置位，
  值=run_id(JSON)；体检 C02 已改为优先消费就绪标记（回退读完成标记兼容）
- **测试**：`test_inference_lock.py` 9 条——纯函数/键构造/**真实 Redis 并发抢锁仅一胜者**/**CAS 属主校验（错误 token 不误删）**/四处接线源断言
- **证据（2026-09-16）**：9/9 通过；admin/celery 模块 import OK；ruff 无新增（B904 数与基线一致）；
  **下一交易日 08:00 推理为首个观察点**（就绪标记写入 + 锁日志）
- **变更文件**：`shared/inference_lock.py`(新)、`script_runner.py`、`celery_tasks.py`、`admin/model_management.py`、`admin/model_management_utils.py`、`scripts/diagnose/health.py`、tests

### T-P1-03 Order/Fill 契约 ✅
- **背景（侦察后按真实缺口收窄）**：`sim_orders` 已有 `price_source/execution_model`（apply_filled 已在写取价来源）；reason 已由 `remarks` 承载——不重复造列
- **本批修复**：① `client_order_id` 落 `sim_orders` 台账（此前注释自述"只写投影"，投影为空时幂等断链）；② `orders`(REAL) 增 `price_source`（成交来源可溯）；③ 两表增 `source`（rebalance/manual/internal/mirror/sltp 来源分类）
- **实现**：`backend/shared/order_contract.py`（signal_contract 同款自愈式、独立事务）；接线三写入点——
  `order_service.create_order`（client_order_id + source←trigger_source）、
  `engine._execute_order`（确定性幂等键 `sim-{run_id}-{symbol}-{side}` + source=rebalance）、
  `execution_stream_consumer._handle_order_filled`（price_source=broker_fill）；两模型同步；`db_init.sql` 同步
- **不加唯一索引**（记 T-P1-04）：投影幂等查全路径未验证前，硬约束会把重复单变成 500
- **测试**：`test_order_contract.py` 9 条（合成器纯函数/迁移幂等/两表 DDL 同步/三写入点源断言/模型字段/来源枚举）
- **证据（2026-09-16）**：9/9 通过；四列已上库（information_schema 确认）；**两表写入冒烟**（BEGIN…INSERT…ROLLBACK：sim_orders 落 client_order_id/source/price_source、orders 落 broker_fill/mirror）通过；ruff 无新增

### T-P1-04 Ledger 契约 ✅
- **内容**：① **成交必落账**——新增真库 E2E 测试（record_trade 买入 → COMMIT → 账户/批次/流水三表齐落校验 → 清理测试租户）；
  体检 C05 升级为**覆盖率检查**（近 7 日成交 vs cash_ledger ref_id，过渡期历史缺口 warn 点名）；② **跨市场串账修复**——
  `simulation_position_lots`/`simulation_cash_ledger` 增 `market` 维度（写入落市场、消费/投影/重建按
  `COALESCE(market,'CN')` 过滤）；`load_projection(market=)` 透传；重建（simulation_manager）按市场、
  EOD 与融券显式 CN、企业行为保持全市场旧行为；③ 引擎重跑幂等去重（`RULE:SIM-DEDUP`，同 run 同标的同方向跳过）；
  ④ `backend/shared/ledger_contract.py` 自愈迁移（安全化三纪律）；db_init.sql 同步
- **测试**：`test_ledger_contract.py` 6 条（含 **真库 E2E**）+ 体检 14 条全绿
- **证据（2026-09-16）**：20/20 通过（E2E 实测三表齐落 market=CN、测试租户零残留）；两表列已上库；ruff 无新增
- **明确未做（记录在案）**：① 账户层仍为合并视图（account_id 带市场段的彻底市场化账户属后续）；
  ② sim_orders `client_order_id` 唯一索引仍未启用（投影幂等查全路径未验证，硬约束会把重复单变 500）；
  ③ runner 专用只读 DB 账号（T-P0-03 遗留）

### T-P1-07 资本注入调整 + 快照市场维度（P0-05 遗留）
新市场账户**首日**的 `today_pnl` 不能把种子算成当日盈利：① 快照表加 `market` 列（前端注释里的"方案 B2"，解决非 CN 面板无市场维度）；② `get_baselines` 按市场对齐日初基线，或按"新账户出现的当日将种子计入基线"。测试：新建市场账户当日 today_pnl ≈ 0。

### T-P1-05 今日交易台后端聚合 API ✅
- **内容**：`GET /api/v1/desk/today`（api 服务）——一屏闭环：管线四步 + 信号（BUY/SELL/HOLD +
  Top5 候选按 rank_pct）+ 执行（SIM/REAL 今日订单合并，带 price_source/client_order_id/origin/reason）+
  盈亏（用户级快照）+ 健康卡。**与体检脚本同源**：pipeline 四步与 health 卡直接映射
  `health.py::CHECKS` 判定（不重复造逻辑）；健康检查同步 IO 走 `asyncio.to_thread`（不阻塞事件循环）；
  `?health=false` 可跳过；**所有块带 `source` 下钻字段**；身份双口径（快照/模拟单=归一 uid，REAL=原始 sub）
- **测试**：`test_desk_today.py` 5 条（pipeline 纯函数/unknown 降级/同源与 to_thread 源断言/路由注册/下钻字段）
- **证据（2026-09-16）**：5/5 通过；**线上冒烟**（重启后 admin token 实调）——pipeline 四步、
  BUY 1040/SELL 843、Top5 候选、盈亏 200.8 万、健康 7 ok/2 warn/1 fail（fail 为已知双键形）全部正确返回

### T-P1-06 调度表统一 ✅（P1 收官）
- **内容**：`backend/shared/scheduler_registry.py` = 全部 11 个周期任务（5 worker + 6 celery）的**唯一事实源**
  （归属/周期/开关环境变量/心跳 TTL/手动重跑命令）；**心跳协议** `qm:sched:hb:{key}`（best-effort 不抛出，
  11 个任务全部接线——漏接一个测试即红）；体检 C07 升级为按注册表逐项判定
  （stale=fail 调度停摆 / missing=warn 过渡 / off 按开关关闭，**决不静默**）；
  `backend/scripts/schedule_ctl.py`：`list`（调度表+心跳实况）/ `run <任务键>`（sim_eod、auto_inference、
  market_sync_dispatch 三个支持手动重跑，`--date` 语义按任务；`--force` 为保留参数并如实提示不造假语义）
- **测试**：`test_scheduler_registry.py` 7 条（注册表完整性/开关纯函数/心跳判定纯函数/best-effort 假 redis/11 任务接线源断言/体检消费/分发表覆盖）
- **证据（2026-09-16）**：21/21 通过；**线上实测**——5 worker 心跳 9-57s 鲜活、celery 侧 dispatch/news×2 心跳 33-45s
  （重启 celery-worker/beat 后）；auto_inference/strategy_lab/backfill 为按日任务，首次触发后自然出现（C07 如实 warn 点名）；
  体检 C07 线上输出"6 项无心跳记录……观察一周期"（过渡期口径）

---

## P2 执行统一（3-4 周）

### T-P2-03 取价契约 ✅
- **内容**：`execution_engine._resolve_fill_price(order, bar, strict_market)` = **取价唯一实现**（两路径共用）：
  ① L0/L1 新鲜实时价直接用（source 如实）；② strict（手动即时市价单）遇非实时拒单（保持 P0-5 语义）；
  ③ 非 strict 降级 bar——`bar.trade_date` 如实标注 `today_bar_close`/`prev_close_bar`，degraded=True +
  `[RULE:PRICE-STALE]` WARNING（**修复"盘中按昨收成交且谎报 local_close"**）；④ 全无拒单。
  `ashare_matcher.MatchConfig.external_price`（解析价喂撮合，滑点/涨跌停钳制不变）；
  `execute_order` 价格段重构为同一 resolver（守卫单实现）；from_bar 结果 price_source 改为真实来源
- **测试**：`test_price_contract.py` 10 条（matcher 覆写/ resolver 六态 / 两路径接线源断言）
- **证据（2026-09-16）**：10/10 通过；**真机探针**——托管路径对 600036.SH 如实输出
  `prev_close_bar / price=41.83 / degraded=True`；手动 strict 保持原拒单话术（local_daily_open → 拒绝）

### T-P2-01 OrderRouter（唯一入口）✅
- **架构决策（细案论证后实施）**：Router = 组合而非重写——即时链委托给线上验证过的
  `SimulationOrderSubmissionService`（锁/幂等/会话窗/投影），Router 补齐：统一请求/结果契约、
  **from_bar 托管模式**（锁→幂等→建单→execute_from_bar→落账）、**镜像收口**、strict_market 分级
- **五路径全部改接**（注释与源断言双重防回退）：托管引擎（bar+mirror，旧内联链与 direct
  execute_from_bar/SimOrder 创建全部移除）/ 沙箱消费者（**修 R6：获得锁+幂等+落账+真单镜像**，
  source=sandbox）/ TDX 滚动 paper（获得锁/涨跌停/费用/落账，source=tdx_rolling）/ internal
  dispatcher（source=hosted/manual，既有镜像通知保留）/ 融券强平（source=forced_liquidation，
  刻意 strict=False 允许如实降级——风险优先不平不掉仓）
- source 域新增：sandbox / tdx_rolling / hosted / forced_liquidation（taxonomy 测试同步）
- `execute_order`/`submit_and_fill` 增 `strict_market` 参数贯穿（默认 True 保 P0-5）
- **测试**：`test_order_router.py` 7 条（strict 解析/校验早退/委托捕获 source+strict/duplicate 映射/
  五路径源断言/防回退）+ 既有 74 条全绿
- **证据（2026-09-16）**：74/74 通过（含 P1 全部套件）；重启后启动干净；体检基线不变

### T-P2-02 撮合规则单实现 ✅
> **实施细案（2026-09-16，先侦察后写）**
> - **侦察结论（费用规则目前 4 份实现）**：① `ashare_matcher` 常量副本（数值与 CN_RULES 相同但独立定义）；
>   ② `market_rules` 只算合计（无过户费分项）；③ `execute_order` 内联 settings 版（分项，env 可覆盖）；
>   ④ `backend/shared/backtest_engine`（**当前无生产调用方**）flat `0.001` 佣金、无最低/印花/过户/手数。
>   涨跌停阈值已收敛（backtest `get_price_limit_threshold` 已委托 `local_market_data.limit_pct`）。
> - **单一事实源 = `market_rules.py`**：`MarketTradingRules` 增 `transfer_fee_rate`（CN 0.00001）；
>   新增 `compute_fee_breakdown(qty, price, side, *, 可覆盖费率) -> (commission, stamp, transfer)`
>   （默认值来自规则，**env/前端 settings 仅作显式覆盖**——"可配置"语义不丢）；`compute_commission` 改为其合计。
> - **三处调用收敛**：matcher（删 4 个费用常量，MatchConfig 默认值改引 CN_RULES，覆盖路径不变）；
>   execute_order（CN 内联块改调 breakdown + settings 覆盖；非 CN 印花税从"合并进佣金"改为独立分项——
>   现金合计不变、sim_trades 分项更正确，属记录改进）；backtest_engine（佣金走 breakdown +
>   最低/印花/过户、**买入/开空按 lot 取整**、现金与成交记录含 total_fee 分项）。
> - **本批不做（记录在案）**：回测 T+1 锁定（需改 portfolio 账本模型，随 T-P2-05 配套）、
>   回测滑点参数化到 bps（现为独立 rate，语义等价）、minibt 引擎接入（归 P7 论证）。
> - **验收 = 规则平价测试**：同一 (symbol, 价格, 数量, 方向) 下 matcher / market_rules / 回测引擎
>   三处费用**逐分相等**（含最低佣金与卖方印花场景）；买入 lot 取整一致；涨跌停阈值同源。
- **落地（2026-09-16）**：`market_rules` 增 `transfer_fee_rate` + `compute_fee_breakdown`（费用唯一实现）
  + `normalize_order_quantity`（申报数量唯一实现，**科创板 200 股起、1 股递增**）；四处收敛并删除 3 份副本
  （matcher 费用常量、rebalance `_floor_to_lot`、回测引擎 flat 佣金）；回测引擎另获：最低佣金/印花/过户、
  买入申报归一、成交记录费用分项（total_fee）。
- **验收（规则平价）**：`test_rule_parity.py` 5 条——matcher vs rules vs **回测引擎费用逐分相等**
  （含最低佣金/卖方印花）、科创板 201 股语义（201 合法/150 拒）、ST 涨跌停 2026-07-06 两侧、
  源断言防第二实现；全量回归 **99/99**。

### T-P2-07 盘后固定价格交易窗口（2026-07-06 新规）✅
**背景（用户提示后查证）**：沪深北交易所 2026-07-06 新规将**盘后固定价格交易扩至全部 A 股与 ETF**
（15:05–15:30、按收盘价、时间优先；申报时间沪 9:30–11:30/13:00–15:30，深/北 9:15–11:30/13:00–15:30；
买入价 < 收盘价 / 卖出价 > 收盘价 为无效申报）；沪市基金收盘改收盘集合竞价（14:57–15:00）。
**落地（2026-09-16）**：
- ① **sim 撮合盘后会话**：`market_rules.session_for_time()` 会话解析唯一入口；`ashare_matcher` 盘后成交价=取价链
  结果、无滑点；`execute_from_bar` 按墙钟推导会话（`_resolve_match_session`）；`execute_order` MARKET 分支盘后
  跳过滑点（LIMIT 分支现语义即盘后申报价规则本身，零改动）；两侧 `[RULE:AFTER-HOURS]` 日志。
- ② **枚举扩展**：`TradingSession.AFTER_HOURS`（共享 schema）；`real_trading_utils` 归一函数 `allow_after_hours`
  （SIM 放行 + 时刻落窗校验 / REAL 显式 400）；`simulation_hosted_scheduler` 会话门 15:05–15:30（常量源自
  market_rules）；前端类型/表单（"盘后"按钮、默认 15:05/15:10、时点边界随会话自适应）/校验 ranges。
- ③ **REAL 不接入**（QMT 盘后通道待核实）：REAL 配置显式拒绝；镜像/轮询维持"15:05 后入队次交易日"保守语义。
- ④ **L0 时段校验**：`is_after_hours_fixed_session` 唯一谓词接进会话推导与调度门两消费点；P5 规则引擎直接引用。
- **顺带修复（回归抓出）**：T-P2-02 收敛时 CN_RULES 漏配 `transfer_fee_rate`，**过户费 0.001% 全链路静默归零**
  （撮合/执行/回测），旧测试 `test_ashare_matcher` 早有断言但因该批未跑此套件未暴露——已补 `0.00001` 修复；
  同批更新科创板过时断言（350 不截断）与 precheck 测试隔离（TDX 桥兜底未 mock，桥在线即假失败）。
- **证据**：新套件 `test_after_hours_fixed.py` **35/35**；`backend/tests` 全量 **1921 passed**（HEAD 基线 1883，
  +35 新增 +3 修复，**集合差零新增失败**，其余 30F/6E 为 HEAD 既有环境/他链路遗留：qlib trade_unit 配置、
  to_qlib 小写命名、训练/版本等）；`services/tests` 591 passed（3 条既有失败与 HEAD 一致）；前端 `tsc --noEmit` 干净；
  容器运行时冒烟：09:31→continuous / 15:10→after_hours_fixed / REAL 拒绝话术 / SIM 放行均正确。
- **边界（记录在案）**：墙钟 15:05–15:30 真机成交需实盘时段自然触发（本轮以注入时刻测试覆盖）；申报窗
  （沪深 9:30/9:15 起可申报）属券商侧语义，模拟不建模；部分成交/队列位置不建模（按序全额）。
**附注**：申报数量新规的科创板部分已在 T-P2-02 落地；**创业板"200 股起"口径待交易所原文核实**
（搜索源与既有认知冲突），代码暂保持 100 整数倍并留出处注释。

> **T-P2-07 实施细案（2026-09-16，先侦察后写）**
> - **侦察结论（触点全图）**：模拟盘两条执行路径 = ① 托管/调仓 `OrderRouter._submit_from_bar → execute_from_bar`
>   （喂 bar 走 ashare_matcher）；② 手动/内部单 `_submit_immediate → order_submission_service → execute_order`
>   （即时链，market/limit 分支内联）。策略时段枚举 = `LiveTradeConfigSchema.TradingSession`(AM/PM)，
>   保存期校验在 `real_trading_utils._normalize_live_trade_config`（SIM/REAL 共用同一函数，调用点
>   `real_trading_lifecycle` 已持有 mode），读取期判定在 `simulation_hosted_scheduler._is_enabled_session`
>   （第二份副本）。L0 时段口径现状 = `trading_session.py`（REAL 侧：QMT 轮询/镜像，15:05 后即非交易时段）。
> - **① 撮合（唯一实现）**：`market_rules.session_for_time()`（从已有 `is_after_hours_fixed_session` 派生）为会话解析
>   唯一入口；`ashare_matcher.match_order` 在 `session=after_hours_fixed` 时成交价 = 取价链结果（external_price 优先 →
>   bar.close）**不加滑点**（固定价格机制语义）；涨跌停/停牌/可卖量/申报数量闸门**保持与连续竞价同向**
>   （涨停收盘不买、跌停收盘不卖——保守口径，队列深度无数据可建模）；打 `[RULE:AFTER-HOURS]` 日志。
>   申报价有效性（买 < 收盘无效）由申报侧承担：`execute_order` LIMIT 分支现语义（买价<市价拒/卖价>市价拒/按市价成交）
>   **即为盘后规则本身**，零改动；MARKET 分支盘后跳过滑点按收盘价成交。
> - **② 枚举扩展**：`TradingSession` 加 `AFTER_HOURS`（共享 schema）；`real_trading_utils` session_ranges 加
>   `AFTER_HOURS: 15:05–15:30`（常量取自 market_rules，不落副本），归一函数加 `allow_after_hours` 参数——
>   SIMULATION 保存放行、**REAL 显式 400**（③ 未核实，fail-closed 带明确话术）；`simulation_hosted_scheduler.
>   _is_enabled_session` 加 AFTER_HOURS 分支（同一常量源）。前端：类型 + 表单 SESSIONS/SESSION_DEFAULTS（"盘后"，
>   默认 sell 15:05 / buy 15:10）+ 校验 ranges。
> - **③ REAL 通道**：**不接入**（QMT 是否支持盘后单未核实）。REAL 配置显式拒绝；镜像/轮询维持现状
>   （15:05 后视为非交易时段 → 入队次交易日补交，保守不丢单）。核实后的接线点：`trading_session.TRADING_SESSIONS`
>   加盘后段 + 本批 `session_for_time` 复用。
> - **④ L0 时段校验**：唯一谓词 = `is_after_hours_fixed_session`（本批接进 ① 会话推导与 ② 调度门两消费点，
>   边界 15:05:00/15:30:00 含）；P5 风控规则引擎落地时直接引用，不建第二实现。
> - **不做（记录在案）**：回测引擎（日线 T 下单 T+1 成交，无日内时段概念）不适用；replay `day_runner` 保持
>   continuous（时光回放不按墙钟）；部分成交/队列位置不建模（时间优先简化为按序全额成交）。
> - **测试**：`backend/tests/test_after_hours_fixed.py`——谓词边界（15:04:59/15:05:00/15:30:00/15:30:01）·
>   matcher 盘后无滑点（买/卖/external_price）+ 闸门回归（涨停买拒/跌停卖拒/停牌/科创板 201）· 墙钟会话推导
>   `_resolve_match_session` · 调度门 AFTER_HOURS 三时刻 · 配置校验 SIM 放行 / SIM 超窗拒 / REAL 拒 ·
>   执行引擎两路径接线源断言；既有回归（rule_parity/backtest_sim_parity/price_contract/matcher/scheduler）全绿。

### T-P2-04 退出规则单一实现 ✅
- **落地（2026-09-16）**：`backend/shared/exit_rules.py` 唯一实现（优先级阶梯：硬止损→止盈→移动→信号→时间；
  触发依据快照留档）；**四处收敛**——TDX `check_sltp_trigger` 纯包装（**零行为漂移**：既有
  `test_tdx_quote_feed`/`test_risk_trigger` 两套件 55 例全过）、风控触发器接入（命中类型映射回原契约）、
  回放 `scan_stop_loss` 接入（low 触发口径不变）；**模拟活盘引擎新增退出评估**（此前完全没有）：
  hosted 周期先评估持仓退出（规则取自策略 execution_config，与实盘隐式止损同源），退出卖单
  `source=sltp` + `[RULE:EXIT]` 自定位日志，与同周期调仓卖单共用幂等键自动去重。
- **v1 边界（记录在案）**：hard_stop + take_profit；trailing/time_stop 需持仓最高价/开仓日历史 →
  T-P2-04b（随 PG lots 集成）；回测 `StopLossManager` 属独立体系（回测专用）暂不动。
- **测试**：`test_exit_rules.py` 9 条全绿；全量回归 183/183；重启干净。
> **实施细案（2026-09-16，先侦察后写）**
> - **侦察结论（退出规则 5 处实现）**：① TDX `check_sltp_trigger`（纯函数，sltp_executor 与桥 daemon 共用）；
>   ② 风控触发器 `risk_trigger_service.evaluate_user_account`（position_stop_loss/take_profit 规则，Redis 配置）；
>   ③ 回放 `proposal.scan_stop_loss`（**日线 low 触发**、跌停钳制、跳空开盘价成交）；④ 回测
>   `StopLossManager`（fixed/trailing/ATR 策略类）；⑤ 策略参数（execution_config.stop_loss 等）。
>   **模拟活盘引擎没有任何退出评估**（持仓只在信号掉出 TopK 时被卖）——这是"持仓管理"的最大缺口。
> - **单一实现 = `backend/shared/exit_rules.py`**：`ExitRuleSet`（hard_stop/trailing/take_profit/time/signal）+
>   `PositionState`（entry/high_water/hold_days/last_price|low_price）+ `ExitDecision`（rule_id/priority/reason/
>   **触发依据快照**）；优先级阶梯：硬止损 > 移动止损 > 止盈 > 信号消失 > 时间止损（先命中先出，快照落档）；
>   触发价输入由调用方选择（实时 last / 日线 low），同阈值同口径。
> - **三处收敛**：`check_sltp_trigger` 改为纯包装（reason 文案保持兼容，桥与执行器零改动）；
>   回放 `scan_stop_loss` 改调 canonical（low 触发语义不变，止盈/移动未来可直接用）；
>   风控触发器评估接入（position_stop_loss/take_profit 与 canonical 同判定）。
> - **模拟活盘接线（v1）**：hosted 周期在调仓**之前**评估持仓退出（规则取自策略 execution_config 风险字段，
>   与实盘隐式止损同源）；退出卖单沿用 `sim-{run}-{sym}-sell` 幂等键 → 与同周期调仓卖单自动去重；
>   v1 支持 hard_stop + take_profit（trailing/time 需持仓最高价/开仓日历史 → 随 T-P2-04b lots 集成，记录在案）。
> - 测试：`test_exit_rules.py`——优先级阶梯纯函数、快照内容、sltp 包装与旧行为逐案对照、三处接线源断言、
>   引擎退出单接线源断言；既有回放/触发器测试回归。
>
> **T-P2-05b（同批）**：
> - ② **市价单不顺延**：回测 `_process_orders` 对不可成交（涨停/跌停/停牌/无行情）的**市价单即时拒单**
>   （限价单保持顺延挂单）——与模拟"当日拒单"语义对齐；平价夹具断言升级为"两侧当日均不成交 + 回测状态=REJECTED"。
> - ① **真实数据平价**：平价夹具增加 QuantDB 真实 bars 变体（2 标的 × ~15 交易日，买/持有/卖），
>   数据不可用时 skip；策略层回放全链路（同一策略对象驱动回测 vs 时光回放）仍列 T-P2-05c。

### T-P2-05 回测-模拟一致性测试 ✅（v1 执行层 diff=0）
- **内容**：`test_backtest_sim_parity.py` 夹具——同一条确定性订单计划（买/卖/最低佣金/科创板 201 股/
  大额卖出印花）分别喂给回测引擎（T 下单→T+1 成交）与模拟执行核 `ashare_matcher`（同滑点、
  canonical 涨跌停/费用/申报归一）；断言逐单**完全一致**（数量/价格 round4/三项费用逐分/成交与否）。
  配套对齐：回测引擎成交价 round(4)（与撮合器逐位对齐，parity 前提）。
- **证据（2026-09-16）**：夹具 2/2 通过（含涨停日两侧均不成交）；全量回归 **127/127**；可进 CI（无外部依赖）。
- **T-P2-05b ✅（2026-09-16 同批落地）**：① **真实数据平价**——夹具新增 QuantDB 真实 bars 变体
  （2 标的 × 12 交易日，买/持有/卖），实测通过（未被 skip）；② **市价单不顺延语义统一**——回测
  `_process_orders` 对不可成交（涨停/跌停/停牌/无行情）的市价单即时拒单（限价单保持挂单顺延），
  与模拟"当日拒单"对齐，平价夹具断言升级为"两侧均不成交 + 回测状态=REJECTED"。
- **T-P2-05c（列后续）**：策略层回放全链路（同一策略对象驱动回测 vs 时光回放 + 真实数据端到端）。

### T-P2-06 模拟盘成交即落账 + 影子对照 ✅
成交落账闭环 + shadow 对照报告（模拟-实盘偏差指标）。

**落地（2026-09-16）**：
- **① 影子对照纯函数唯一实现** `backend/shared/shadow_compare.py`：`pair_orders`（三源键配对：cid 列∪order_id∪
  remarks 内嵌，符号/方向错配标记）· `compute_price_deviation`（bps 方向归一：买贵/卖便宜均=正成本，
  mean/median/p95 |abs|）· `compute_fill_stats`（成交率=真实/模拟量、部分成交、拒单）· `compute_slippage_realization`
  （实现滑点 vs 配置 bps）· `compute_tracking_error`（共同交易日对齐→日收益差 mean/std，年化 ×√244，
  <3 天=insufficient 且不除零）· `build_shadow_report`（覆盖率+样本不足如实标注）。
- **② 采集与日报** `backend/services/trade/services/shadow_compare_service.py`：`collect_day_pairs`（sim_trades
  executed_at 窗口 ⋈ sim_orders；orders mir-% ⋈ 成交价）；`collect_equity_series`（**双键形归一探测**：
  模拟侧归一整型 "620601" ↔ 实盘侧补零 "00620601"，取第一个有数据形式防双计）；日报落 Redis
  `mirror:shadow:{date}`（TTL 30 天），**空日也落"跑过且为空"的证据**；常驻任务每日 15:15（`MIRROR_SHADOW_ENABLED`），
  心跳入调度注册表（新增 `mirror_shadow` + 补登记遗漏的 `dual_book` JobSpec，两张表可手动重跑）。
- **③ 收敛既有**：`/qmt-mirror/reconcile` 端点改为委托 `collect_day_pairs`（**修复只认 remarks 前缀、
  漏 engine/OrderRouter 镜像路径的配对缺口**；响应契约保持，前端零改动）；删除端点内重复的取价/时间窗实现。
- **④ 落账闭环加固**：`reconcile_service` 零差异落当日 clean 证据行（进程内去重）→ C06 可判"对账零差异"；
  统计新增 `clean/ledger_empty`（Redis 有账但 PG 台账空**显式可见**，不再静默跳过——本机实测 ledger_empty=4）；
  C05 新增幂等键重复扫描（fail 级，`classify_cid_duplicates`）；QMT poller 回填成交补 `price_source=broker_fill`
  （与桥接链路同源）；**实测修复 `normalize_status` 枚举入参静默降级**（str(enum) 查表失败→SUBMITTED 丢成交）。
- **⑤ 交易台**：desk 新增 `shadow` 块（读最近日报不重算，不可用时如实标注不伪造）。
- **实测修复（联调发现）**：`_raw_client` 客户端形态兼容——CLI 手动重跑传入原生客户端（无 `.client` 包装）
  曾致"跑成功但落盘静默 no-op"；schedule_ctl 重跑统一走 trade 客户端（与 worker 同库）。
- **记录在案（不做）**：`sim_orders` 唯一索引仍缓办（T-P2-08：各写路径 IntegrityError→重复语义需先统一，
  否则硬约束变 500——本批先以 C05c 重复扫描保可见性）；策略级跟踪误差（快照表无 strategy 维度）；
  历史 Redis-only 虚拟成交回填（无源）。
- **证据**：新套件 `test_shadow_compare.py` **18/18** + `test_shadow_compare_service.py` **13/13**（含真库 E2E：
  60bps 偏差/成交率/双键形跟踪误差/clean 行落盘去重）；`backend/tests` 全量 **1957 passed**（基线 1921，
  +36 新增、修复 1 条冻结日期旧测试，**集合差零新增失败**）；services/tests 591 passed 与基线一致；
  实机重启后 `mirror_shadow`/`dual_book` 心跳新鲜、CLI 重跑→日报→desk 读取闭环通过。

> **T-P2-06 实施细案（2026-09-16，先侦察后写）**
> - **侦察结论（触点全图）**：镜像链路 `mirror_virtual_fill` 建真单
>   `orders.client_order_id='mir-{base}'`（base=sim cid｜sim_order_id｜run-symbol-side），模拟侧关联见
>   `sim_orders.client_order_id` 列（T-P1-03）或 remarks `client_order_id=`（dispatcher 路径）。
>   已有实现三处：① `/qmt-mirror/reconcile` 端点（逐单价格滑点+费用差，**只认 remarks 前缀的模拟单**
>   ——engine/OrderRouter 路径配对遗漏）；② `dual_book_reconciliation_task` 每日 15:10 双轨**数量**对账
>   （Redis `mirror:reconcile:{date}`+告警）；③ `reconcile_service` Redis↔PG 30s 对账（**零差异不落报告**
>   ——C06 恒"无对账报告"，健康时反而无证据）。本机实测存量：镜像真单 62（压测产物）、REAL 成交 73、
>   sim PG 台账近空（历史 Redis-only 遗留）；sim 资金快照 user '1'、real 日快照 user '00000001' ——
>   **两侧 user_id 键形不同，对照必须以归一 uid 连接**。QMT poller 回填成交**不写 price_source**（标注缺口）。
> - **① 纯函数唯一实现 `backend/shared/shadow_compare.py`**：`pair_orders`（base 键配对：matched/
>   sim_only/real_only）· `compute_price_deviation`（成交价偏差 bps：买+贵=正成本，n/mean/median/p95/
>   abs_mean）· `compute_fill_stats`（成交率=real_filled/sim_filled、部分成交、拒单）·
>   `compute_slippage_realization`（实现滑点 vs 配置 bps）· `compute_tracking_error`（按日对齐共同交易日→
>   日收益差 std/mean，年化×√244；<2 天=insufficient）· `build_shadow_report`（组装+覆盖率+样本不足标注，
>   **永不除零**）。
> - **② 采集与日报 `backend/services/trade/services/shadow_compare_service.py`**：`collect_day_pairs`
>   （sim_trades executed_at 窗口 ⋈ sim_orders，real orders mir-% ⋈ 成交价 average_price）；`collect_equity_series`
>   （simulation_fund_snapshots ↔ real_account_ledger_daily_snapshots，**双方 uid 归一整型**）；
>   `run_shadow_compare` 挂进 dual_book 每日循环（独立开关 `MIRROR_SHADOW_ENABLED` 默认 on），
>   落 Redis `mirror:shadow:{date}`（TTL 30 天），unexplained 复用现有通知。
> - **③ 收敛既有**：`/qmt-mirror/reconcile` 端点改复用 `collect_day_pairs`（修 remarks-only 漏配；
>   响应 items/summary 契约保持）。**不建第二实现**。
> - **④ 落账闭环加固**：reconcile_service 零差异时按日写 clean 摘要行（进程内去重）→ C06 有"对账零差异"
>   证据；stats 增 `ledger_empty` 计数（Redis 有账/PG 台账空显式可见，不静默 skip）；QMT poller 回填补
>   `orders.price_source='broker_fill'`；`sim_orders` 部分唯一索引 (tenant,user,client_order_id) WHERE NOT NULL
>   （order_contract 自愈迁移安全模式；先核全部插单路径过幂等预检）。
> - **⑤ 交易台**：`desk.py` 新增 `shadow` 块——实时滚动窗（默认 20 交易日）指标 + 当日日报可用性，
>   按既有 `_collect_*` 模式（带 source 下钻）。
> - **⑥ 测试（机构级）**：纯函数数学口径全套（偏差方向/分位/成交率/跟踪误差对齐与不足样本/覆盖率分类/
>   空样本）· 真库 E2E（造 sim 单+成交+镜像单 → run → 断言报告 → 清理，ledger_contract 模式）·
>   reconcile clean 行与 C06 联动 · qmt_mirror 端点回归（engine 路径配对）· trade/main 接线 tripwire ·
>   测试隔离修复（`test_dual_book_reconciliation` 冻结日期 bug——load_skips 写死 20260911）。
> - **不做（记录在案）**：策略级跟踪误差（快照表无 strategy 维度，账户级先行）；对照面板自动刷新
>   （日报 + 交易台按需计算已够）；历史 Redis-only 时代虚拟成交回填（无源）。

---

## P3 策略收敛（2-3 周）

### T-P3-01 策略注册表状态机（DRAFT→VERIFIED→SIM→LIVE）+ 版本 + 参数锁 ✅
### T-P3-02 `strategy_storage.delete` async 修复 + E2E ✅

**落地（2026-09-16，T-P3-01/02 同批）**：
- **状态机唯一实现** `backend/shared/strategy_lifecycle.py`（纯函数）：规范词表
  DRAFT/VERIFIED/SIM/LIVE/ARCHIVED；**存量归一**（ACTIVE/REPOSITORY→VERIFIED、
  LIVE_TRADING→LIVE，实测 88 行 ACTIVE 全部平移）；迁移表含幂等（同状态回写=no-op）；
  **启动门禁** `can_start`：SIM 须 VERIFIED（回测验证）、REAL 须 SIM（模拟证据，门槛细则归 T-P3-05）。
- **存储层**（`strategy_storage.py`）：`get()` 补回 status/version（此前 SELECT 取了却不返回）；
  `update_lifecycle_status` 经迁移校验（非法迁移告警拒绝，不再静默改写）；
  **save UPDATE 分支重写**——① 补丁语义（description/config/execution_config 仅显式提供才覆盖，
  修复"局部更新抹配置"族缺陷）② 版本递增（代码/参数/执行配置实质变化才 +1）③ **参数锁**
  （SIM/LIVE 改内容必须携带 `expected_version`=当前版本，否则 StrategyLockedError/VersionConflictError）。
- **运行时驱动**（此前回写通道 `_schedule_status_writeback` 已建但全仓无调用方→死代码）：
  `start_trading` 接线（启动成功→SIM/LIVE；启动前过 `can_start` 门禁，sys_ 模板/文件上传豁免）；
  `stop_trading` 接线（→VERIFIED）。
- **API 兼容**：`_normalize_base_status` 扩展新词表（VERIFIED→repository、SIM/LIVE→live_trading，
  前端可见值不变）；update 端点透传 `expected_version`、锁冲突→409。
- **T-P3-02 delete 修复**：`strategy_storage.delete` 行数诚实（不再无条件 True）+ **运行中拒绝**
  （SIM/LIVE→ValueError 直出话术，防悬空引用）+ 归档行可清理（旧实现经 get() 过滤归档永远删不掉）；
  死代码 `user_strategy_loader.delete_strategy` 三缺陷修复（未 await 协程被当成功返回 / user_id 硬编码 "0" /
  结果不反映行数）；`list()` 对无法解析 user_id 防御返回空。
- **存量测试修复**（初版起即红、未入过回归）：`backend/shared/tests/test_strategy_storage.py`
  6 条失效（打桩约定/行结构/同步调用 async 假绿）全部修正 + 新增运行中删除守卫用例，16/16。
- **记录在案**：LIVE→SIM 直接回退不允许（经 VERIFIED）；AI-IDE 回测→VERIFIED 触发属 T-P3-04；
  晋级量化门槛总表 T-P3-05；前端策略页"8 种状态"词表待随 T-P3-04 前端批次对齐。
- **证据**：新套件 `test_strategy_lifecycle.py` 22/22（纯函数）+ `test_strategy_lifecycle_storage.py`
  6/6（含真库 E2E：版本递增/参数锁/迁移矩阵/删除守卫/归档清理）；受影响套件 91/91；
  `backend/tests` 全量 **1985 passed**（上批基线 1957，+28、零新增失败）；`backend/shared/tests`
  18（修复 6 条旧红，剩 1 条 request_logging 预存在红）；services/tests 591 与基线一致。

### T-P3-07 市场时段参数化 + 美股模板系列（缺口解锁）✅
**背景**：T-P3-06 记录的缺口"美股/期货模板系列为零"，阻塞项 = 时段校验/调度为 A 股写死钟点。
**设计决策（关键口径）**：**live_trade_config 的时间一律为策略市场本地时钟**——A股 "14:45"=北京、
美股 "15:50"=美东、期货夜盘 "01:30"=北京。本地钟点不随夏令时漂移（校验零 DST 复杂度），
调度器按市场时区换算"现在"（夏令时由 zoneinfo 吸收）。
**落地（2026-09-16）**：
- **唯一实现 `backend/shared/market_sessions.py`**（纯函数）：五市场时段表（CN 含盘后固定价格 15:05–15:30
  与 market_rules 常量同步断言守护；US 常规 09:30–16:00 + 盘后 16:00–20:00；HK 09:30–12:00/13:00–16:00；
  FUTURES 日盘 09:00–15:00 + 夜盘 21:00–02:30（跨午夜，简化口径）；CRYPTO 7×24）+ 市场时区
  （Asia/Shanghai · Asia/Hong_Kong · America/New_York）+ 交易日历映射（XSHG/XHKG/XNYS）+ 市场键归一
  （a_share/us_stock/hong_kong… 未知保守回落 CN）+ 跨午夜 in_session_hhmm。
- **消费方全链市场参数化**：实盘配置校验（session_ranges_local + 跨午夜支持）；托管调度器
  （_should_trigger/_next_scheduled_trigger/_parse_started_at 全部按市场时区；交易日/调仓日指数按市场
  日历 XNYS/XHKG/XSHG）；撮合会话推导 `_resolve_match_session(market=…)`——**盘后固定价格仅 CN 启用**
  （美股/港股回落 regular，不误用收盘价固定成交）。
- **美股模板系列 ×9**（us_standard_topk / bigcap_core / weighted_core / alpha_weighted / momentum /
  adaptive / stop_loss / ls_topk / **extended_hours**）：ET 本地钟 live_defaults（收盘前 15:50/15:58；
  extended_hours 演示盘后 19:50/19:58 时段）+ 专业文档（汇率/PDT/盘后流动性提示）+ execution_defaults。
- **前端**：marketConfig 增时段表（与后端同口径镜像）+ 时钟口径标签；LiveTradeConfigForm/Wizard/
  校验函数市场驱动（时段按钮/默认时点/区间钳制/跨午夜放宽）；RealTradingPage 传当前市场；
  TradingSession 词表加 NIGHT；**已构建部署**（main-hd9YImQB.js）。
- **证据**：`test_market_sessions.py` **29/29**（含 US 夏令时/冬令时换算、期货跨午夜、调度门、
  美股触发链北京 03:50→美东 15:50、实盘校验按市场钟）；模板三套件 28/28；AST 闸门 **95/95**；
  backend/tests 全量 **2021 passed**（上批基线 1991，+30，零新增失败）；services/tests 591 与基线一致；
  实机重启干净 + 美股触发链冒烟通过。
- **记录在案**：期货夜盘为简化口径（商品差异化时段待品种级规则）；美股 REAL 通道（tiger/ib）的时段
  校验与 PDT 限制待通道核实后接入（当前 fail-closed 仅模拟）。


- **T-P3-03** 旧 5 格式 → 2 格式转换器/下架清单
- **T-P3-04** AI-IDE 接入统一回测（结果落库 + 触发 verified）
- **T-P3-05** 晋级门槛总表（唯一事实源，模型/策略/环境三方引用）

### T-P3-06 策略模板库专业化补齐（用户点名 2026-09-16）✅
**背景**：用户反馈"有些没有内容的模板需要补充、整体需专业机构级优化适配平台"。
**清点结论（86 套模板）**：as01–as50（A股旗舰系列）已含完整文档/参数/回测记录；缺内容的为
——**minibt×11 参数面板全空**（json params=[]）+ live/defaults/tips 全空；legacy×10 与 hk×15
缺 execution_defaults（风控默认落平台兜底）与部分 tips；薄文档（10–13 行 docstring）。
**落地（2026-09-16）**：
- **json 补齐 37 套**：minibt×11（指标参数/交易常量全量入参，含 SYMBOL/SIZE/COMMISSION 与
  区间约束 + 研究型口径 tips 三条 + live_defaults A股标准预填）；legacy×10（execution_defaults
  按风格定值：StopLoss -5% 收紧、value_growth -10% 放宽、long_short -6% 等 + tips）；
  hk×15（execution_defaults 按港股波动特性：小盘 -5%/-10%、高股息 -3%/-10% 等）。
- **文档专业化 26 套**（legacy×10 + hk×15 的 .py 重写 docstring）：as 风格——市场口径（T+1/涨跌停
  或港股 T+0/无涨跌停）、核心逻辑、调仓/持仓/换手、参数指引、使用指引（sys_ 前缀实盘）、风险提示；
  hk 系列统一注明"尾盘 15:50/15:58 调仓（live_defaults 预填）"与汇率/流动性提示。
- **质量闸门（机构测试）** `backend/tests/test_strategy_template_quality.py` 7 条：零空壳
  （params 非空 + default∈[min,max]）· exec_defaults 落运行时校验区间（与 _normalize_execution_config
  同源）· live_defaults 时段合法 · tips 非空 · 描述唯一 · .py 配对 · 真实加载器全量 0 解析错误。
  **防复发：未来任何空壳模板入目录即测试红。**
- **验证**：AST 安全闸门全库扫描 **86/86 零误杀**；三套件混跑 28/28（修复加载器单例缓存
  跨用例污染——加载器测试用 monkeypatch 临时目录污染单例，质量闸门改用新实例 + 前后失效）。
- **发现并记录的缺口（后续项）**：**美股/期货模板系列为零**（平台已有 QuantUS 数据/训练/推理与
  多市场个股终端）；阻塞项 = `live_trade_config` 时段校验为 A 股口径（AM/PM 固定钟点），
  美股时段（北京时间夜间）无法通过校验——需先做"市场时段参数化"（含 hosted 调度会话门），
  再补 us_*/futures_* 模板系列；本批**不做假**（不写无平台支撑的 live_defaults）。

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