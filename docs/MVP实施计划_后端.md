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
| P3 策略收敛 | 7/7 ✅（T-P3-05 门槛总表随 T-P4-06 体检接入定稿） | 策略全生命周期 E2E |
| P4 选股收敛+评估 | 6/6 ✅（五卡 + 体检九项 + 三处接入全落地） | Scanner 替换旧链；体检九项上线 |
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


### T-P3-03 旧 5 格式 → 2 格式转换器/下架清单 ✅
**落地（2026-09-16）**：
- **五格式全库实测清点**：① STRATEGY_CONFIG 声明式 75 行（执行器：回测中心/托管/SDK——保留格式 A）；
  ② minibt DSL 2 行 + 9 行空壳克隆（执行器：AI-IDE py3.12——保留格式 B）；③ handle_data 聚宽风
  **存量 0 行**但活跃于两处 AI 兜底生成路径（无任何执行器）；④ 空/桩残壳 12 行（11 空 code +
  1 桩 `# New Strategy`）；⑤ 沙箱 on_tick / AI-IDE 脚本型 0 行进库（运行时形态，记录边界不转换）。
- **唯一实现 `shared/strategy_format.py`**：`classify_strategy_code` 五分类 + `is_executable_format`
  闸门 + `build_scaffold_strategy_code` 声明式占位骨架。
- **下架**：两处 AI 兜底生成（strategy_service / json_utils）**停止产出 handle_data 伪代码**，
  改产可执行的声明式骨架（含"占位，请补全"标注，过 AST 闸门）。
- **审计脚本** `scripts/strategy_format_audit.py`（--json；下架清单）；
  **修复脚本** `scripts/repair_strategy_code_formats.py`（默认 DRY-RUN；--apply；--only-ids）
  ——按 name 精确匹配模板回填 .py + sha256，无匹配不猜测列人工。
- **实机执行**：审计 89 行 → 回填 11 行（2 legacy + 9 minibt 克隆）→ 复审计 **88/89 可执行**
  （77 声明式 + 11 minibt），余 id=39 桩「8888」（DRAFT，人工清单）。
- **证据**：`test_strategy_format.py` 6/6（五形态/骨架过闸门与注入安全/兜底下架源断言/dry-run
  契约/真库回填幂等 E2E 含抖动降级 skip）；backend/tests 全量 **2028 passed** 零新增失败。

### T-P3-04 AI-IDE 接入统一回测（结果落库 + 触发 verified）✅
**落地（2026-09-16）**：
- **结果落库**：核实 `QlibBacktestService.run_backtest` 运行时内部已持久化
  （`BacktestPersistence.save_run → qlib_backtest_runs`，AI-IDE/minibt/同步 API/优化链路共用），
  无缺口——本项记录为"已由服务内部承担"。
- **触发 verified**：AI-IDE 容器 runner 回测成功（status=completed）后调用
  `_mark_strategy_verified_on_success(strategy_id)`（来源 `AI_IDE_BACKTEST_STRATEGY_ID` 环境变量，
  仅数字 strategy_id 触发，sys_ 模板/纯代码模式不受影响；失败仅告警不阻断回测输出）。
- **状态一致化（修 T-P3-01 发现的分裂）**：`mark_as_verified` 重写——`is_verified=TRUE`
  与 **DRAFT→VERIFIED 状态机迁移联动**（此前只置 is_verified 不动 status，会让"回测已通过但
  status=DRAFT"的策略被 T-P3-01 的 SIM 启动门禁误拒）；SIM/LIVE 重复回测不降级、ARCHIVED 保持；
  行数诚实（目标不存在 → False）。celery 回测任务路径（tasks.py）经同一函数自动联动。
- **记录在案**：标记仍为"回测无异常即通过"（不看收益指标）——量化门槛（DSR/夏普/回撤下限）
  归 T-P3-05 晋级门槛总表；AI 兜底模板（无策略行为）不触发。
- **证据**：`test_strategy_lifecycle_storage.py` 8/8（新增真库 E2E：DRAFT→mark→VERIFIED 联动、
  SIM 不降级、不存在 False、幂等；executor 接线源断言）；backend/tests 全量 **2023 passed**
  （上批 2021，+2，零新增失败）。

### T-P3-05 晋级门槛总表（唯一事实源，模型/策略/环境三方引用）✅
**代码唯一事实源**：`backend/shared/backtest_health.py` 顶部常量区（改常量即改全平台口径，
文档与测试同源断言）；门禁执行点 `real_trading_lifecycle.py /start`（`promotion_gate()` 纯函数）；
应急开关 `HEALTH_GATE_ENABLED=false` 全局旁路（记日志）。

| 晋级 | 门槛 | v1 口径与数据条件 |
|------|------|------------------|
| DRAFT → VERIFIED | 回测成功 + is_verified（T-P3-04 已有） | 不变 |
| VERIFIED → SIM | **回测体检结论 ∈ {A, B}**（B 放行但门禁信息标注收益来源）；无体检记录/L（运气嫌疑）/E（证据不足）→ 拒绝 | 体检由回测完成后自动生成（T-P4-06 ①）；拒绝信息带证据与建议 |
| SIM → LIVE | 同 SIM 门槛 + **SIM 运行 ≥ 20 交易日** + **模拟↔真单跟踪误差 ≤ 15%**（年化，TE_ann） | SIM 天数 = 首启打点起算的交易日（Redis `strategy:sim_start:{t}:{u}:{sid}`，NX 保留最早；**v1 近似**：停启不累计）；跟踪误差取影子对照日报（`te_ann_bps`），**无数据（无真单/共同日不足）→ 只提示不拦**（如实降级）；历史启动无打点 → 同上不拦 |

**与设计文档差异（记录）**：设计 §6.3 示例报告把"1 年样本 → E 区"视作常态——本表据此不设
"短样本豁免"；用户对 E 的正确解法是延长回测区间，而非绕过门禁。

**测试**：`test_backtest_health.py::test_promotion_gate_verdict_matrix / _real_mode_thresholds`
（A/B/L/E/未体检 + REAL 天数/跟踪误差矩阵）；门禁接线源断言防漂移。

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

### T-P4-01 Scanner SPI + 模型信号扫描器迁入（等价性验证）✅
> **实施细案（2026-09-16，先侦察后写）**
> - **侦察结论**：现有"模型信号选股"核心 = `inference_backtest_service._select_stocks_daily`
>   （个股分数区间+主板+ST/涨跌停+3 天趋势过滤，近乎纯函数：day_scores/industry_map/config/
>   price_day/history_scores → [{symbol,score,industry,trend}]）+ `_compute_industry_signals`
>   （行业 Top1/avgTop1/强行业数，纯函数）+ `_market_state`；/selection 端点（1874 行）是其薄封装。
> - **SPI 设计**：`shared/scanner_spi.py`（纯函数唯一实现）——`Opportunity`（含 sources/strength/
>   score 0-100/horizon/evidence/expiry/state，对齐设计 §三）+ `merge_opportunities`（多源合并：
>   同标的并 sources、共振加分、同源冷却期、过期出池）+ 扫描器注册表（声明式：id/name/market/
>   frequency/scope/开关，仿 scheduler_registry）。
> - **模型信号扫描器（移入不改写）**：`services/engine/scanners/model_signal_scanner.py` 直接调用
>   `_select_stocks_daily`（**复用而非复制**——单一实现原则，等价性由构造保证+测试锁定），
>   输出 Opportunity：strength=rank_pct 分位（T-P1-01 列）、score=round(strength×100)、
>   evidence={fusion_score, industry, trend}；批级 meta 附行业信号与市场状态（扫描器只发现，
>   买不买由策略决定——设计铁律）。
> - **IO 适配**：`load_model_signal_snapshot(trade_date, market)`（engine_signal_scores +
>   申万行业映射；纯 scan 不碰 DB）+ `run_scan` 编排 + CLI `scripts/run_scanner.py`。
> - **等价性验证（机构级）**：① 确定性夹具——同快照喂 `_select_stocks_daily` 与扫描器，
>   逐字段相等（symbol/score/industry/trend）；② **真库等价**——取真实交易日信号跑双侧，
>   symbol 集合完全一致（无数据 skip）；③ merge 纯函数（共振/冷却/过期）；④ 注册表不变量。
> - **边界（记录）**：阈值仍为现绝对口径（T-P4-03 改分位，等价性基线不变）；机会池持久化/
>   盘中扫描/其余六路扫描器归后续批次。

**落地（2026-09-16）**：
- **SPI 唯一实现** `shared/scanner_spi.py`：`Opportunity`（sources/strength 0..1/score 0-100/horizon/
  evidence/expiry/state）+ `ScannerSpec` 注册表（注册即生效、独立开关 `SCANNER_MODEL_SIGNAL_ENABLED`、
  仿 scheduler_registry）+ `merge_opportunities`（同标的并 sources、**多源共振 +8/源封顶 100**、
  同源冷却期、过期出池，纯函数）+ 稳定序列化。
- **模型信号扫描器** `services/engine/scanners/`：**移入不改写**——`scan_model_signals` 直接复用
  `_select_stocks_daily`（单一实现，等价性由构造保证）+ `_compute_industry_signals/_market_state`
  批级证据（entry_gate 只呈现不拦截）；strength=rank_pct、score=round(rank×100)；
  `model_signal_loader`（**自动取最新写入身份**——不硬编码键形，双键形教训：日更写 00000001、
  历史写 system）+ `runner.run_scan` 编排 + CLI `scripts/run_scanner.py`。
- **等价性验证（机构级）**：确定性夹具逐字段对照（symbol 序列/score/industry/trend 全等且夹具
  有区分度断言防假绿）+ **真库双侧对照**（最新信号日 9/15，5189 只）双路径一致；merge/冷却/
  共振/过期纯函数 10 条。
- **实测修复（等价性工具抓到）**：`_select_stocks_daily` 在**分数带内为空**时因"空 DataFrame ×
  空布尔索引丢列"（pandas 行为）KeyError 崩溃——真库空带日（量纲错位期）可复现；已加空集提前
  返回 + 回归测试（该函数为 /selection 与回测引擎共用核心）。
- **实机冒烟**：CLI 全路扫描 9/15 → 市场状态=熊市 avgTop1=0.0117、0 选中——与已知量纲问题一致
  （T-P4-03 分位化修复的靶子）；链路（装载→扫描→合并→呈现）端到端通。
- **证据**：`test_scanner_spi.py` 10/10 + `test_model_signal_scanner.py` 7/7（含真库等价）；
  backend/tests 全量 **2045 passed**（+17，零新增失败）；services/tests 与基线一致。
- **边界（记录）**：机会池持久化/盘中扫描/其余六路扫描器归后续批次；v1 扫描器仅注册表一条。

### T-P4-03 阈值分位化全量替换 + 量纲回归测试 ✅
> **实施细案（2026-09-16，先侦察后写）**
> - **根因**：选股链硬编码绝对分数阈值（个股带 [0.10,0.12]、行业 avgTop1≥0.09/0.06），
>   而模型分数实际分布 [-0.048,0.012]（9/15 实测 5190 只 0 只落带）→ **恒空仓**。
>   设计铁律：**阈值只允许引用 rank_pct（分位），绝对分数只做诊断展示**（统一交易栈 §4.1）。
> - **唯一实现 `shared/signal_thresholds.py`**：`QuantileThresholdProfile`（全 0..1 分位空间：
>   个股带 [p98, 1.0]、行业入场 avgTop1 分位 ≥ p90、强行业 ≥ p98、强行业数 ≥2——参数可调）
>   + `resolve_thresholds`（纯函数：任意分数序列 → ThresholdSet，含审计留痕）；
>   **量纲恒定 by construction**：秩变换后阈值永远 0..1，任何模型尺度都不可能再错位。
> - **扫描器集成（不改核心）**：分位模式下把快照的 score 域替换为 rank_pct 域（分数→秩），
>   配置带替换为分位带 → 复用 `_select_stocks_daily` 同一实现跑（单一实现保持）；行业门/
>   强行业数在秩域重算；meta 留 threshold 审计。`--mode absolute` 保留 A/B 与等价基线。
> - **量纲回归测试（机构级）**：**尺度不变性**属性测试——同一票（分数 ×137.5 / ÷1000 /
>   平移）下分位模式选股结果逐一相同（绝对模式归零作对照）；真库非空验证（9/15 分位模式
>   选出 >0 只——恒空仓修复的活证据）；resolver 边界（n=0/1、并列、单调）。
> - **不做（记录）**：`model_training` 页信号卡的自适应启发（独立展示面）留 T-P4-02 退休
>   波次统一；回测引擎绝对口径保留（场景回测 A/B 基线）；模拟 TopK 链的 min_score 死配置
>   随 T-P4-02 处理。

**落地（2026-09-16）**：
- **唯一实现** `shared/signal_thresholds.py`：`QuantileThresholdProfile`（个股带 [p98, max]、
  行业入场 avgTop1≥p90、空仓 p70、强行业 p98，全 0..1 分位空间）+ `resolve_thresholds`
  （分数分布 → 阈值集，含审计留痕；空输入 None；并列/单值边界）。
- **扫描器集成（不改核心）**：`scan_model_signals(mode=)` — 分位模式把**分布推得的阈值**
  （仍是分数空间，尺度等变）替换进 `StrategyConfig`（band/entry/exit/strong_min）后复用
  `_select_stocks_daily` 同一实现；核心 `_compute_industry_signals` 增 `strong_threshold`
  可注入参数（默认 0.10 保存量等价）；runner/CLI 默认 `--mode quantile`、absolute 保留 A/B。
- **量纲回归（机构级核心）**：**尺度不变性**属性测试——分数 ×137.5 / ÷1000 / 平移 ±0.5/±25
  下分位模式选股逐一同集；**绝对模式 ×137.5 归零对照**（证明测试有区分度）；
  resolver 尺度等变/边界纯函数测试。
- **实机修复取证**：9/15 全市场（分布 [-0.048, 0.012]）——绝对模式 0 选中（恒空仓复现），
  **分位模式选出 5 只**（600648.SH/600817.SH/600983.SH/603357.SH/603676.SH，各带 rank 分位/
  行业/融合分证据链），CLI 实机输出留档；真库非空断言进测试（218 行）。
- **测试卫生（同批）**：两条真库用例加"用后关池"（asyncpg 连接绑定事件循环，pytest-asyncio
  每测试独立 loop → 残留池致同进程后续真库测试跨循环 skip）——**24/24 零 skip 确定性通过**。
- **证据**：`test_signal_thresholds.py` 7/7（含真库非空）+ 扫描器/SPI 套件 17/17 回归；
  backend/tests 全量 **2052 passed**（+7，零新增失败）。

### T-P4-02 旧 `/selection` 三层过滤退休（声明 + 引导到新入口，Claude skills 同步改）✅
> **实施细案（2026-09-16，先侦察后写）**
> - **消费面侦察**：活消费方 = Claude skills（stock-market-analysis 全市场扫描/大盘环境、
>   trading-agents 个股研判）经 `/api/v1/selection/daily`；前端选股面板（StockPickingPanel 等）
>   自诊断起即死代码（无 import），本批不动、记 refactor 清单；后端另有 ai_strategy 的
>   `/selection/parse|execute`（同名不同物，无关）。
> - **新入口**：`GET /api/v1/scanner/daily`（engine 服务）：run_scan 服务化——date/strategy/mode
>   参数，**身份无信号自动回落最新写入身份**（`identity_fallback` 如实标注），响应含
>   market_state（分位口径）/entry_gate/thresholds/opportunities 证据链。
> - **/selection/daily 恒空仓同步修复 + 退休声明**：保留响应契约（skills 平滑过渡），阈值切换
>   到 shared/signal_thresholds（与扫描器同源）；市场状态改分位口径 `market_state_quantile`
>   （绝对阶梯 avgTop1≥0.12/0.10/0.09/0.06 在窄分布模型下恒"熊市"）；响应加
>   `deprecated/replacement` 字段 + HTTP `Deprecation`/`Link: successor-version` 头。
> - **model_training 页统一**：`compute_market_signals` 的 wide-scale 探测启发（80/50/30 或
>   绝对 0.10/0.09/0.06）替换为同一 resolve_thresholds——**页/选股链/扫描器三方口径归一**。
> - **Claude skills 同步改**：stock-market-analysis（SKILL.md+2 参考文档）与 trading-agents
>   SKILL.md 的 curl 示例切到新入口（注明旧路径过渡期仍可用）；双副本同步纪律（memory）。
> - **测试**：market_state_quantile 纯函数；新端点接线/身份回落源断言；/selection 分位切换 +
>   deprecated 字段（真库直调 handler：market_state 非"熊市"且 candidates 非空——恒空仓修复
>   在旧面同步生效的活证据）；compute_market_signals 纯函数分位口径测试。

**落地（2026-09-16）**：
- **新入口 `GET /api/v1/scanner/daily`**（engine，已注册上线/OpenAPI 确认）：run_scan 服务化——
  strategy/date/mode 参数、**身份无信号自动回落最新写入身份**（`identity_fallback` 如实标注）、
  响应含 market_state/entry_gate/thresholds/opportunities 证据链。
- **/selection/daily 恒空仓同步修复 + 退休声明**：阈值切到 shared/signal_thresholds（与扫描器同源）；
  市场状态 `market_state_quantile`（绝对阶梯在窄分布下恒"熊市"——实机取证：修复前
  candidates=0，修复后 5 只且 market_state 与 entry_gate 自洽）；响应 `deprecated/replacement`
  字段 + HTTP `Deprecation`/`Link: successor-version` 头。实机直调：candidates 非空 ✓ 头齐备 ✓。
- **三方口径归一**：`compute_market_signals`（训练页）删两套启发（wide-scale 80/50/30 与窄分布绝对
  0.10/0.09/0.06）改同一 resolve_thresholds；扫描器 meta 的 market_state 也切分位口径
  （实机探针抓到"entry_ok=True 但状态=熊市"的自相矛盾——已修）。
- **Claude skills 同步迁移**（11 处）：stock-market-analysis（SKILL.md+2 REFERENCES）与
  trading-agents SKILL.md 的调用示例→新入口，含响应结构注释更新（candidates→opportunities/
  分位口径 market_state）；旧路径注明"弃用、过渡期仍可用"。
- **证据**：`test_selection_retire.py` 4/4（含真库直调旧端点：非空候选+退休三件套）+
  纯函数（窄分布不再恒空仓）；backend/tests 全量 **2055 passed**（+3，零新增失败）；
  实机：新端点直调（错误身份回落✓5 只机会）、engine 重启后路由上线确认。
- **记录（refactor 清单）**：前端死码选股面板（StockPickingPanel/ModelScoreResearch/
  NegativeScorePanel + stockPickingService）随 refactor-cleaner 批次清理。

- **T-P4-02** 旧 `/selection` 三层过滤退休（声明 + 引导到新入口，Claude skills 同步改）
**落地（2026-09-16）**：
- **SPI 唯一实现** `shared/scanner_spi.py`：`Opportunity`（sources/strength 0..1/score 0-100/horizon/
  evidence/expiry/state）+ `ScannerSpec` 注册表（注册即生效、独立开关 `SCANNER_MODEL_SIGNAL_ENABLED`、
  仿 scheduler_registry）+ `merge_opportunities`（同标的并 sources、**多源共振 +8/源封顶 100**、
  同源冷却期、过期出池，纯函数）+ 稳定序列化。
- **模型信号扫描器** `services/engine/scanners/`：**移入不改写**——`scan_model_signals` 直接复用
  `_select_stocks_daily`（单一实现，等价性由构造保证）+ `_compute_industry_signals/_market_state`
  批级证据（entry_gate 只呈现不拦截）；strength=rank_pct、score=round(rank×100)；
  `model_signal_loader`（**自动取最新写入身份**——不硬编码键形，双键形教训：日更写 00000001、
  历史写 system）+ `runner.run_scan` 编排 + CLI `scripts/run_scanner.py`。
- **等价性验证（机构级）**：确定性夹具逐字段对照（symbol 序列/score/industry/trend 全等且夹具
  有区分度断言防假绿）+ **真库双侧对照**（最新信号日 9/15，5189 只）双路径一致；merge/冷却/
  共振/过期纯函数 10 条。
- **实测修复（等价性工具抓到）**：`_select_stocks_daily` 在**分数带内为空**时因"空 DataFrame ×
  空布尔索引丢列"（pandas 行为）KeyError 崩溃——真库空带日（量纲错位期）可复现；已加空集提前
  返回 + 回归测试（该函数为 /selection 与回测引擎共用核心）。
- **实机冒烟**：CLI 全路扫描 9/15 → 市场状态=熊市 avgTop1=0.0117、0 选中——与已知量纲问题一致
  （T-P4-03 分位化修复的靶子）；链路（装载→扫描→合并→呈现）端到端通。
- **证据**：`test_scanner_spi.py` 10/10 + `test_model_signal_scanner.py` 7/7（含真库等价）；
  backend/tests 全量 **2045 passed**（+17，零新增失败）；services/tests 与基线一致。
- **边界（记录）**：机会池持久化/盘中扫描/其余六路扫描器归后续批次；v1 扫描器仅注册表一条。

### T-P4-03 阈值分位化全量替换 + 量纲回归测试 ✅
> **实施细案（2026-09-16，先侦察后写）**
> - **根因**：选股链硬编码绝对分数阈值（个股带 [0.10,0.12]、行业 avgTop1≥0.09/0.06），
>   而模型分数实际分布 [-0.048,0.012]（9/15 实测 5190 只 0 只落带）→ **恒空仓**。
>   设计铁律：**阈值只允许引用 rank_pct（分位），绝对分数只做诊断展示**（统一交易栈 §4.1）。
> - **唯一实现 `shared/signal_thresholds.py`**：`QuantileThresholdProfile`（全 0..1 分位空间：
>   个股带 [p98, 1.0]、行业入场 avgTop1 分位 ≥ p90、强行业 ≥ p98、强行业数 ≥2——参数可调）
>   + `resolve_thresholds`（纯函数：任意分数序列 → ThresholdSet，含审计留痕）；
>   **量纲恒定 by construction**：秩变换后阈值永远 0..1，任何模型尺度都不可能再错位。
> - **扫描器集成（不改核心）**：分位模式下把快照的 score 域替换为 rank_pct 域（分数→秩），
>   配置带替换为分位带 → 复用 `_select_stocks_daily` 同一实现跑（单一实现保持）；行业门/
>   强行业数在秩域重算；meta 留 threshold 审计。`--mode absolute` 保留 A/B 与等价基线。
> - **量纲回归测试（机构级）**：**尺度不变性**属性测试——同一票（分数 ×137.5 / ÷1000 /
>   平移）下分位模式选股结果逐一相同（绝对模式归零作对照）；真库非空验证（9/15 分位模式
>   选出 >0 只——恒空仓修复的活证据）；resolver 边界（n=0/1、并列、单调）。
> - **不做（记录）**：`model_training` 页信号卡的自适应启发（独立展示面）留 T-P4-02 退休
>   波次统一；回测引擎绝对口径保留（场景回测 A/B 基线）；模拟 TopK 链的 min_score 死配置
>   随 T-P4-02 处理。

**落地（2026-09-16）**：
- **唯一实现** `shared/signal_thresholds.py`：`QuantileThresholdProfile`（个股带 [p98, max]、
  行业入场 avgTop1≥p90、空仓 p70、强行业 p98，全 0..1 分位空间）+ `resolve_thresholds`
  （分数分布 → 阈值集，含审计留痕；空输入 None；并列/单值边界）。
- **扫描器集成（不改核心）**：`scan_model_signals(mode=)` — 分位模式把**分布推得的阈值**
  （仍是分数空间，尺度等变）替换进 `StrategyConfig`（band/entry/exit/strong_min）后复用
  `_select_stocks_daily` 同一实现；核心 `_compute_industry_signals` 增 `strong_threshold`
  可注入参数（默认 0.10 保存量等价）；runner/CLI 默认 `--mode quantile`、absolute 保留 A/B。
- **量纲回归（机构级核心）**：**尺度不变性**属性测试——分数 ×137.5 / ÷1000 / 平移 ±0.5/±25
  下分位模式选股逐一同集；**绝对模式 ×137.5 归零对照**（证明测试有区分度）；
  resolver 尺度等变/边界纯函数测试。
- **实机修复取证**：9/15 全市场（分布 [-0.048, 0.012]）——绝对模式 0 选中（恒空仓复现），
  **分位模式选出 5 只**（600648.SH/600817.SH/600983.SH/603357.SH/603676.SH，各带 rank 分位/
  行业/融合分证据链），CLI 实机输出留档；真库非空断言进测试（218 行）。
- **测试卫生（同批）**：两条真库用例加"用后关池"（asyncpg 连接绑定事件循环，pytest-asyncio
  每测试独立 loop → 残留池致同进程后续真库测试跨循环 skip）——**24/24 零 skip 确定性通过**。
- **证据**：`test_signal_thresholds.py` 7/7（含真库非空）+ 扫描器/SPI 套件 17/17 回归；
  backend/tests 全量 **2052 passed**（+7，零新增失败）。

### T-P4-02 旧 `/selection` 三层过滤退休（声明 + 引导到新入口，Claude skills 同步改）✅
> **实施细案（2026-09-16，先侦察后写）**
> - **消费面侦察**：活消费方 = Claude skills（stock-market-analysis 全市场扫描/大盘环境、
>   trading-agents 个股研判）经 `/api/v1/selection/daily`；前端选股面板（StockPickingPanel 等）
>   自诊断起即死代码（无 import），本批不动、记 refactor 清单；后端另有 ai_strategy 的
>   `/selection/parse|execute`（同名不同物，无关）。
> - **新入口**：`GET /api/v1/scanner/daily`（engine 服务）：run_scan 服务化——date/strategy/mode
>   参数，**身份无信号自动回落最新写入身份**（`identity_fallback` 如实标注），响应含
>   market_state（分位口径）/entry_gate/thresholds/opportunities 证据链。
> - **/selection/daily 恒空仓同步修复 + 退休声明**：保留响应契约（skills 平滑过渡），阈值切换
>   到 shared/signal_thresholds（与扫描器同源）；市场状态改分位口径 `market_state_quantile`
>   （绝对阶梯 avgTop1≥0.12/0.10/0.09/0.06 在窄分布模型下恒"熊市"）；响应加
>   `deprecated/replacement` 字段 + HTTP `Deprecation`/`Link: successor-version` 头。
> - **model_training 页统一**：`compute_market_signals` 的 wide-scale 探测启发（80/50/30 或
>   绝对 0.10/0.09/0.06）替换为同一 resolve_thresholds——**页/选股链/扫描器三方口径归一**。
> - **Claude skills 同步改**：stock-market-analysis（SKILL.md+2 参考文档）与 trading-agents
>   SKILL.md 的 curl 示例切到新入口（注明旧路径过渡期仍可用）；双副本同步纪律（memory）。
> - **测试**：market_state_quantile 纯函数；新端点接线/身份回落源断言；/selection 分位切换 +
>   deprecated 字段（真库直调 handler：market_state 非"熊市"且 candidates 非空——恒空仓修复
>   在旧面同步生效的活证据）；compute_market_signals 纯函数分位口径测试。

**落地（2026-09-16）**：
- **新入口 `GET /api/v1/scanner/daily`**（engine，已注册上线/OpenAPI 确认）：run_scan 服务化——
  strategy/date/mode 参数、**身份无信号自动回落最新写入身份**（`identity_fallback` 如实标注）、
  响应含 market_state/entry_gate/thresholds/opportunities 证据链。
- **/selection/daily 恒空仓同步修复 + 退休声明**：阈值切到 shared/signal_thresholds（与扫描器同源）；
  市场状态 `market_state_quantile`（绝对阶梯在窄分布下恒"熊市"——实机取证：修复前
  candidates=0，修复后 5 只且 market_state 与 entry_gate 自洽）；响应 `deprecated/replacement`
  字段 + HTTP `Deprecation`/`Link: successor-version` 头。实机直调：candidates 非空 ✓ 头齐备 ✓。
- **三方口径归一**：`compute_market_signals`（训练页）删两套启发（wide-scale 80/50/30 与窄分布绝对
  0.10/0.09/0.06）改同一 resolve_thresholds；扫描器 meta 的 market_state 也切分位口径
  （实机探针抓到"entry_ok=True 但状态=熊市"的自相矛盾——已修）。
- **Claude skills 同步迁移**（11 处）：stock-market-analysis（SKILL.md+2 REFERENCES）与
  trading-agents SKILL.md 的调用示例→新入口，含响应结构注释更新（candidates→opportunities/
  分位口径 market_state）；旧路径注明"弃用、过渡期仍可用"。
- **证据**：`test_selection_retire.py` 4/4（含真库直调旧端点：非空候选+退休三件套）+
  纯函数（窄分布不再恒空仓）；backend/tests 全量 **2055 passed**（+3，零新增失败）；
  实机：新端点直调（错误身份回落✓5 只机会）、engine 重启后路由上线确认。
- **记录（refactor 清单）**：前端死码选股面板（StockPickingPanel/ModelScoreResearch/
  NegativeScorePanel + stockPickingService）随 refactor-cleaner 批次清理。

- **T-P4-02** 旧 `/selection` 三层过滤退休（声明 + 引导到新入口，Claude skills 同步改）
- **T-P4-03** 阈值分位化全量替换 + 量纲回归测试
### T-P4-04 买入前 K 线过滤（移植 KHunter 4 规则）✅
> **实施细案（2026-09-16，先侦察后写）**
> - **四规则（设计 §事前·形态 + 策略表达示例）**：① 距低点涨幅 rise_from_low≤50%（20 日低点，
>   防追已大涨）② 开盘跳空 open_gap≤4%（vs 昨收，防高开接盘）③ BIAS5≤7%（5 日乖离，防短线
>   过热）④ 量能确认 vol_ratio≥0.7（当日量/前 5 日均量，防极度缩量假突破）。
> - **唯一实现 `shared/buy_filters.py`**（纯函数）：`BuyFilterConfig` + 表达式解析
>   （`rise_from_low<=50%`/`open_gap<=4%`/`bias5<=7`/`vol_ratio>=0.7` 与策略 spec
>   `risk.buy_filters` 同形，未知键报错不静默）+ `evaluate_bar_filters`（逐规则证据）
>   + `apply_buy_filters`（opportunities × bars → 保留/拒绝+原因）。
> - **数据装载** `services/engine/scanners/daily_bars.py`：QuantDB hub `fetch_series`
>   （**不复权**日线——前复权价在除权日跳变会污染 gap/bias）；符号数据不足→该标的**拒买**
>   （fail-closed）；基础设施不可用→整步跳过并 meta 如实标注（fail-loud，不静默通过）。
> - **接线**：runner 在 scan 后对 model_signal 机会做买入前过滤（纯函数层，不动 scan 等价性）；
>   meta 记 `buy_filters`（配置/通过/拒绝明细）；CLI 展示。
> - **测试**：四规则边界（通过/拒绝各态）+ 组合顺序 + 解析器（规范形/未知键报错）+
>   真库装载（9/15 实选 5 只取日线 ≥6 根）+ runner 接线源断言。

**落地（2026-09-16）**：
- **唯一实现 `shared/buy_filters.py`**（纯函数）：`BuyFilterConfig`（四规则默认=手册推荐值：
  距低点涨幅 ≤50%/20 日、开盘跳空 ≤4%、BIAS5 ≤7、量能比 ≥0.7）+ **表达式解析器**
  （`rise_from_low<=50%` 等策略 spec 同形；未知键/反向约束 ValueError 响亮失败）+
  `evaluate_bar_filters`（逐规则证据快照）+ `apply_buy_filters`（拒绝明细留痕）；
  **数据不足 fail-closed 拒买**。
- **数据装载** `services/engine/scanners/daily_bars.py`：QuantDB hub `fetch_series`
  （**不复权**日线——前复权价除权日跳变会污染 gap/bias）；hub 不可用→整步跳过并 meta 如实标注。
- **接线**：runner 扫描后对机会执行买入前过滤（纯函数层，scan 等价性不受影响）；
  meta 落 `buy_filters`（配置/通过/拒绝明细）；CLI 展示过滤摘要与拒绝原因。
- **实机取证**：9/15 实选 5 只全链路——真实不复权日线四规则评估
  `passed=5, rejected=[]`（决策日非过热标的，全过为合理结果；拒绝路径由边界测试覆盖）。
- **证据**：`test_buy_filters.py` 12/12（四规则边界/解析器/apply/fail-closed/真库取线）；
  P4 全系五套件 **40/40**；backend/tests 全量 **2067 passed**（+12，零新增失败）。

- **T-P4-04** 买入前 K 线过滤（移植 KHunter 4 规则）
### T-P4-05 五张评分卡 + 回测体检九项（`scripts/eval/` + `eval_scores` 表）✅
> **实施细案（2026-09-16，先侦察后写；分批实施）**
> - **批次划分**：**05a（本批）= 回测体检九项核心**（统计工具箱 + 四分类判定 + 报告 + CLI + 测试）；
>   05b = 五张评分卡（因子/模型/策略/每日选股/账户）+ `eval_scores` 表 + EOD 任务；06 = 三处接入。
> - **九项实现（scipy 1.15 已备）**：① 因子回归（CAPM α/t/R²，风格因子可选注入）
>   ② PSR（Bailey-LdP 公式，偏度峰度校正）③ **DSR**（试验次数 N 去胀；试验方差缺省用
>   本策略 Sharpe 抽样方差近似并如实标注）④ MinTRL（目标置信 95%，返回天数/年）
>   ⑤ PBO/CSCV（T×N 参数矩阵，S=10 块组合，IS 最优的 OOS 分位 <0.5 占比）
>   ⑥ Block Bootstrap（块长 √n，B=1000，年化收益/Sharpe 置信区间）
>   ⑦ 收益集中度（剔 Top5 日后复利重算，杀 alpha 即嫌疑）⑧ regime 分段（指数 60 日
>   趋势代理 牛/熊/震荡，逐段存活）⑨ 成本敏感性（换手 × 费率上浮，成本后显著性）。
> - **四分类判定（优先级 E→L→B→A，对齐设计 §六表）**：样本 < MinTRL → E；
>   DSR<0.95 或 bootstrap CI 跨 0 或集中度杀 alpha → L；alpha 不显著（t≤2）或
>   alpha 占收益 <30% → B；全过 → A。可信度分 0-100 由五项分量合成（显著性/DSR/集中度/
>   regime/样本充分度，权重 30/25/15/15/15），全部证据留档。
> - **不做（记录）**：试验次数 N 自动取自参数扫描记录（R2 扫描器落地后接线；当前 CLI --trials）；
>   风格因子序列输入接口预留（数据侧后续）；PDF/图形报告归前端批次。
> - **测试**：九项纯函数（公式性质/边界/已知解析解）+ PBO 噪声矩阵≈0.5 + 四分类制造用例
>   （A/B/L/E 各一）+ **真库 sanity**（上证指数当"策略"跑体检 → 应判 B：R² 高、alpha 不显著）。

**落地·05a 回测体检九项（2026-09-16）**：
- **`backend/scripts/eval/stat_tools.py`**（九项纯函数）：factor_regression（CAPM α/t/R²、风格因子可注入、
  **完美拟合退化守卫**——残差方差≈0 时 t 判 0 不给伪显著）/ psr / **dsr**（N 试验去胀；试验方差缺省用
  抽样方差近似并标注）/ min_trl / **pbo_cscv**（S=10 块组合、IS 最优 OOS 分位 <0.5 占比）/
  block_bootstrap / return_concentration / regime_split（指数 60 日趋势代理）/ cost_sensitivity。
- **`health_check.py`**：九项编排 + **四分类判定**（优先级 E→B(beta解释)→L→A，含"alpha 显著但占比
  <30%→B"档）+ 可信度分（30/25/15/15/15）+ §6.2 文本报告 + CLI（--index/--nav-file/--benchmark/--trials/--json，
  真库指数序列装载）。
- **实机 sanity（上证指数当"策略"）**：R²=0.86、beta=0.74、alpha t=0.26 不显著 → 正确识别 beta 主导
  特征；1 年样本 < MinTRL 66 年 → E 区（数学诚实：1 年数据证明不了任何事）；regime 牛 26/熊 21/
  震荡 142 天全分段。
- **测试**：`test_eval_stat_tools.py` **16/16**（九项性质与解析解 + A/B/L/E 制造用例全通——
  B 用确定性完美 beta、L 用"剔 Top5 杀 alpha"与"短样本×5000 试验去胀"两种、E 用低 SR 短样本 +
  真库 sanity）；backend/tests 全量 **2083 passed**（+16，零新增失败）。
- **05b 待办**：五张评分卡（因子/模型/策略/选股/账户）+ `eval_scores` 表 + EOD 评分任务；
  T-P4-06 三处接入（回测后自动体检/晋级门禁/月度复检）；试验次数 N 自动取自参数扫描记录。
- **05b-1 细案（2026-09-16）**：① `shared/eval_scoring.py` 打分方法统一引擎（§三：阶梯/分位映射、
  winsorize 5/95、加权总分、红线封顶 59、评级 A/B/C/D、低置信 †——维度缺失时按剩余权重归一
  并如实标注）；② `shared/eval_contract.py` eval_scores 自愈迁移（契约安全三纪律）；
  ③ `scripts/eval/daily_selection.py` **每日选股评分卡**（§2.4 五维：事前质量/事后验证
  （T+1..T+H 真实收益回填，前向数据不足→pending † 不假填）/一致性（执行偏差，无当日交易→
  维度缺省归一）/校准（月度，v1 记 insufficient）/覆盖）；CLI --date/--save/--json。
  权重 v1 默认 25/35/15/15/10（设计未定权重，文档化为可调 v1 口径）。
  剩余四卡（因子/模型/策略/账户）与 EOD 接线 → 05b-2。

**落地·05b-1 评分引擎 + eval_scores + 每日选股评分卡（2026-09-16）**：
- **打分方法统一引擎** `shared/eval_scoring.py`：winsorize 5/95 + 分位/阈值映射（双向）
  + 加权合成（**维度缺失按剩余权重归一**并标注）+ 红线封顶 59 + 评级 A/B/C/D + 低置信 †。
- **eval_scores 契约** `shared/eval_contract.py`：自愈迁移安全三纪律（to_regclass 预检零 DDL
  快路径 + lock_timeout + 不阻断）；UNIQUE 幂等 upsert；DATE 参数类型守卫。
- **每日选股评分卡** `scripts/eval/daily_selection.py`（§2.4 五维，v1 权重 25/35/15/15/10
  文档化可调）：事前质量（rank_pct 强度阈值映射+行业分散+板块状态）/ **事后验证**（T+1..T+H
  真实超额，**前向数据不足 → pending † 不假填**）/ 一致性（执行偏差；无记录缺省归一）/
  校准（v1 记 insufficient）/ 覆盖（门/入选合规矩阵）。
- **实机双案例**：9/8 完整评测 **64.3 分 C**（质量 99.7 但事后超额仅 28.8——真实市场反馈；
  缺省维度权重归一）；9/15 pending 案例 **69.9 分 C†**（† 标注待回填）；覆盖维度如实旗标
  "门关但 scanner 仍呈现"的 v1 语义（scanner 只发现不决策，计划门在策略层）。
- **测试**：`test_eval_daily_scorecard.py` **10/10**（引擎纯函数/契约纪律/五维矩阵/真库 E2E：
  9/8 事后可评 + 落表幂等重写 + 清理）；backend/tests 全量 **2093 passed**（+10，零新增失败）。
- **05b-2 待办**：因子/模型/策略/账户四卡 + EOD 评分任务接线（调度注册表）+ 试验次数 N 自动取
  自参数扫描记录；T-P4-06 三处接入随其后。
- **05b-2 实施细案（2026-09-16，先侦察后写）**：
  - 四卡权重沿用设计 §2.1/2.2/2.3 表（因子 30/20/15/20/15；模型 30/25/15/15/15；策略
    20/20/15/15/20/10）；账户卡 §2.5 未定权重 → v1 默认 30/20/25/25（暴露/归因/风控事件/资金效率）
    文档化可调；各卡维度数据不满足前置（样本/序列缺失）一律 **None + insufficient 如实标注**（权重归一）。
  - 实现模板复用 05b-1：`scripts/eval/<card>.py`（纯打分函数可单测 + IO 采集 + CLI --save）+ eval_scores
    落表（object_type=factor|model|strategy|account）。
  - **EOD 任务**：`scripts/eval/run_all.py` 顺序跑五卡 → trade/main 新 worker（`EVAL_SCORES_WORKER_ENABLED`
    默认 true，交易日 16:00 后 60s 轮询，仿 shadow_compare）+ 调度注册表 JobSpec `eval_scores` + 心跳。
  - 数据源判定（侦察结论）→ 见下方落地记录。

**落地·05b-2 四卡 + EOD 汇总 + worker（2026-09-16）**：
- **因子卡** `scripts/eval/factor_card.py`（§2.1 权重 30/20/15/20/15）：预测力 **|RankIC|/|ICIR|**
  （红线 |ICIR|<0.2；**负 IC 强因子标 direction=inverted 按强度计分**——反转即可用，修掉"负 IC 判 0"
  的假阴性；实机发现并修复）· 稳定性 子样本 IC 方差 + **半衰期**（缺失/NaN 视界跳过，不得当 0
  ——防"未测"误判"已衰减"）· 独立性 源 `correlation.{factors,matrix}` 矩阵直读（**修掉把矩阵当
  字典读导致永远缺省**的 bug）· 质量闸门 PFS/DH v1 缺省 · 覆盖均值。
- **模型卡** `scripts/eval/model_card.py`（30/25/15/15/15）：OOS 预测力直接可算（红线 IC<0.02 或
  ICIR<0.3）；**两代元数据归一**（新 `metrics.test_rank_ic/icir` / 旧 `performance_metrics.test.mean_ic/icir`
  ——alpha158 实机验证）；分层/稳健/滚动/换手 v1 如实缺省；ensemble 无 metrics 不评分（组件各自评）。
- **策略卡** `scripts/eval/strategy_card.py`（20/20/15/15/20/10）：收益（窗口超额+绝对 0.6/0.4）、
  风险（MDD≤-50% 红线 + Calmar）、稳定性（月度胜率 + 连续三月负红线）实算；成本/一致性/容量 v1 缺省；
  结果文件被清理的历史行标 skipped（非异常）。
- **账户卡** `scripts/eval/account_card.py`（§2.5 未定权重 → v1 30/20/25/25 暴露/归因/风控/资金效率）：
  暴露=前5集中度+单票上限（红线>50%）+申万行业集中度；归因=**v1 简化口径**（净值窗口超额+盈利贡献
  集中度，Brinson 待接线，detail 如实标注）；风控事件=risk_events 状态计数+拒单（红线 failed≥3；
  **机制未启用 → 缺省**，不把"无记录"当"无事件"）；资金效率=现金拖累阶梯。**双 ID 守卫**：原始键直读
  （00000001 ≢ 1 台账，非规范键形不借位不重复计分）；非 CN 市场不误用 CN 净值序列。空账户 skipped。
- **EOD 汇总** `scripts/eval/run_all.py`：五卡顺序跑，**单卡异常隔离**（一张崩不影响其余，errors 汇总）；
  worker `services/trade/services/eval_scores_service.py`（EVAL_SCORES_WORKER_ENABLED 默认 true，
  16:00 后 60s 轮询，成功写 `eval:scores:done:{date}`，心跳入注册表）+ `schedule_ctl run eval_scores`
  手动重跑；`/app` 单进程共享 DB 池。
- **实机**：五卡全链 **落分 30，异常 0，4.97s**（因子 20 / 模型 2 / 策略 5 / 账户 2 / 选股 1）；
  账户 1:CN 77.8 B（27 持仓）、999:CN 15.2 D（99.6% 现金）；eval_scores 落表核对通过。
- **测试**：`test_eval_cards.py` **20/20**（四卡纯函数 + 两代元数据 + 双 ID/日期回看回归 + 单卡隔离 +
  worker 配置 + 接线断言 + 真库 E2E 三件）；backend/tests 全量回归见提交记录。
- **05b 余项**：试验次数 N 自动取自参数扫描记录（R2 扫描器接线）。

**落地·T-P4-06 体检三处接入（2026-09-16）**：
- **接入层唯一实现** `shared/backtest_health.py`：`evaluate_equity_curve`（曲线→九项报告，复用
  health_check 唯一实现，禁止复制公式）· `evaluate_for_window`（按**曲线窗口**取基准/regime——
  历史回测窗口在当下之前，不是"最近 N 天"）· `promotion_gate`（门禁纯函数）·
  `attach_backtest_health` / `schedule_health_check`（best-effort 后台，绝不阻塞回测落库）。
- **① 回测完成自动体检**：钩子挂 `BacktestPersistence.save_run`（status=completed 且有净值曲线）→
  报告落 `result_json.health`（证据卡，schema 加 `health` 字段可经 API 下钻）+ 策略回测同时落
  `eval_scores`（object_type=**strategy_health**，object_id=策略 id，inputs_version 记 backtest_id/
  evidence_source）；`strategy_id` 从 Celery 任务与 minibt 两条持久化链透传。
- **② 晋级门禁**：`/start` 端点 SIM/LIVE 晋级前 `promotion_gate`——**A/B 放行（B 标注收益来源）、
  L/E/未体检拒绝**（拒绝信息带证据+建议）；REAL 另查 SIM 交易日（首启打点 NX 保留最早）与
  影子对照跟踪误差（无数据如实降级只提示）。**注意行为变化**：存量策略未体检即启动模拟会被
  拒绝并提示重跑回测（设计 §6.3 强制口径；`HEALTH_GATE_ENABLED=false` 应急旁路）。
- **③ 月度复检**：`scripts/eval/health_recheck.py`（证据源优先级：活跃策略的**模拟盘真实净值**
  → 体检留档所指回测曲线 → 皆无记 skipped 不造假）+ worker `health_recheck_service.py`
  （每月 1–7 日窗口、3600s 轮询、`health:recheck:done:{YYYY-MM}` 幂等、心跳入注册表）+
  `schedule_ctl run health_recheck` 手动重跑；**结论退化（A/B→L/E）→ Redis 告警键**
  `health:recheck:alert:*`（TTL 90 天）+ ERROR 日志（通知中心接线留前端批次）。
- **实机验收**：真类 `BacktestPersistence.save_run` 全链（合成回测行）→ result_json.health
  **A/100**（窗口 2024-01-02→2024-10-27，真库基准+regime）+ eval_scores 留档回读 + 清理；
  非法窗口如实降级 E + 告警日志（不造假）。
- **测试**：`test_backtest_health.py` **9/9**（门禁矩阵/四分类确定性夹具 A·B·L·E/短曲线 None/
  SIM 打点 NX/交易日日历回退/复检纯函数/**三处接线源守卫**/真库 E2E 两件）；
  scheduler 注册表测试同步（health_recheck 心跳接线源 + 重跑分发表）。
- **遗留收口（2026-09-16 二批）**：
  - **试验次数 N 自动取自参数扫描记录**：`resolve_sweep_evidence`（`shared/backtest_health.py`）——
    按 `base_request_json->>'strategy_id'` 关联该策略最近一次 **completed** 网格优化
    （`qlib_optimization_runs`）→ N=total_tasks（DSR 去胀）+ 由 `all_results_json` 逐试验净值
    构建 **PBO 的 T×N 矩阵**（有则必跑）；无记录/无策略 → 如实缺省（n_trials_source=default）。
    回测挂钩与月度复检同源接入。
  - **证据卡前端展示**：`HealthEvidencePanel.tsx`（体检结论页签）——四分类徽章（A 红/B 蓝/L 琥珀/
    E 灰）+ 可信度分 + 理由/建议 + **九项明细行**（不足项如实显示原因，不隐藏）；已并入
    高级分析模块（`EnhancedAdvancedAnalysisModule`），`npm run typecheck` 零错误、
    `scripts/deploy_frontend.sh` 构建部署到 quantmind-web 容器。
- **测试补强**：`test_backtest_health.py` 扩至 **12/12**（+PBO 矩阵纯函数 + 扫描记录真库 E2E：
  合成 optimization run → N=40 去胀 + 矩阵 → attach 体检读取 optimization_run 源 → 清理）。
  - **FE-E 数据出口**：`/api/v1/eval/*`（`services/api/routers/eval_scores.py`，eval_scores 唯一
    读取面）——scores 网格（latest_only）/ scores/history（升序）/ health/{sid}（最新+历史+
    **门禁预演**与执行点同源）/ object-types；可见性=租户共享行+本人私有行；只读；HTTP 实机 200。

- **T-P4-05** 五张评分卡 + 回测体检九项（`scripts/eval/` + `eval_scores` 表）
- **T-P4-06** 体检三处接入（回测后/晋级门禁/月度复检）✅ 见上方落地记录（2026-09-16）

---

## 全局 DoD（每个任务）

1. 代码 + 单测（或断言脚本）+ 验收证据（命令与输出）三者齐全；
2. 涉及契约的改动跑契约测试；涉及口径的跑一致性测试；
3. 完成即勾选本文件，写一行"证据摘要"（命令/结果）；
4. 提交信息带任务 ID。