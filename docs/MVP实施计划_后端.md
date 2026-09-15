# MVP 实施计划 · 后端（勾选即进度）

> 版本：v1（2026-09-15）　配合：`docs/统一交易栈_设计方案.md` §12（MVP 定义）
> **使用方式**：每个任务是完成态最小单元——`代码 + 测试 + 验收全过` 才可勾选 `[x]`。任务 ID 全局唯一，提交信息引用 ID（如 `fix(P0-01): ...`）。
> 状态图例：`[ ]` 未开始　`[~]` 进行中　`[x]` 完成　`[-]` 推迟（须写原因）

---

## 总览看板

| 阶段 | 进度 | 完成定义 |
|---|---|---|
| P0 止血+维护基建 | 10/10 ✅ | 安全问题清零；体检脚本可跑；回归进 CI |
| P1 契约化 | 4/6 | 四契约落地；交易台数字可下钻 |
| P2 执行统一 | 0/6 | 回测-模拟一致性 diff=0 |
| P3 策略收敛 | 0/5 | 策略全生命周期 E2E |
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

### T-P1-01 Signal 契约增列
`engine_signal_scores` 加 `market/rank_pct/source/signal_ts`（**已实现并上库**：自愈式迁移 `shared/signal_contract.py`、两写入端接线、`db_init.sql` 同步、设计文档 §4.1 已对齐 rank_pct 口径）；回填脚本 `backend/scripts/backfill_rank_pct.py`（幂等分窗 UPDATE，与 PG percent_rank() 同口径，有对照测试）。下游读点全改为 rank 口径（**待办：下游 rank 化**）。测试：`test_signal_contract.py` 11 条（纯函数/PG 对照/写入端源断言/迁移幂等）+ 回填校验（抽样对账，待回填完成后补）。

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