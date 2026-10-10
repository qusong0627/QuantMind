# engine_signal_scores 写入语义与巡检 runbook（审计 M1 / T4-3）

> 一句话：**判「写侧是否还在写」看 `signal_ts`，绝不看 `created_at`。**
> `created_at` 是「首次落库」不是「最后写入」——upsert 有意不刷它，用它判活跃会
> 得出假「停更」结论（2026-10-09 审计实测踩坑；本 runbook 固化正确口径与巡检模板）。

## 1. 写入模型

- 表 `engine_signal_scores`：日频批量与盘中实时**同表**，靠 `source` 区分
  （`realtime`=热集盘中推理；`batch`/`inference_script`=日频批次）。
- 两个写入器，**同一 upsert 语义**：
  - `backend/services/engine/routers/realtime_contract.py` → `mark_signal_ready`（实时主路）
  - `backend/services/engine/inference/script_runner.py`（批量/推理脚本路）
- 冲突键：`(tenant_id, user_id, trade_date, symbol, model_version, feature_version, run_id)`
  —— 同键重写走 `DO UPDATE`：
  - **刷**：分数/方向/`signal_ts`（=本周期写入时刻）
  - **不刷**：`created_at`（=首次落库时刻，之后冻结）
- 实时节奏：每周期发布**整个热集**（`realtime_service`，cadence 默认 15s）。
  因此 `created_at` 的分布 = 「各行当日首次进热集的时间窗」，与写活动无关。

## 2. 巡检 SQL 模板（盘中每 30 分钟；P0-4 观察单固化）

主判据（lag 应秒-分钟级；`newest_touch` 持续前移=在写）：

```sql
SELECT count(*) AS rows,
       min(created_at) AS first_insert, max(created_at) AS last_insert,
       min(signal_ts)  AS oldest_touch, max(signal_ts)  AS newest_touch,
       now() - max(signal_ts) AS lag
FROM engine_signal_scores
WHERE trade_date = CURRENT_DATE AND source = 'realtime';
```

判读：
- `newest_touch` 随 15s 级周期前移、lag 秒-分钟级 → 健康（`created_at` 不动是正常的）。
- `newest_touch` 冻结而引擎进程在跑 → 真停写：转看旁证 1/2。
- 收盘末轮（~15:07）之后 lag 增长 = 正常（非交易时段不写）。

旁证 1（与 upsert 语义无关的心跳——每发布周期刷）：

```sql
SELECT run_id, status, updated_at FROM engine_feature_runs
ORDER BY updated_at DESC LIMIT 5;
```

旁证 2（Redis）：`qm:realtime:infer:status`（`last_cycle_at` 是否前移）；
`qm:realtime:infer:ledger:<YYYYMMDD>`（`LLEN` 与最后一条 ts）。

## 3. 已知陷阱与边界

- **created_at 假停更**（审计 M1）：2026-10-09 实测 608 行 `created_at` 只有
  09:16:00 / 09:17:15 两个瞬时，`signal_ts` 一路写到 15:07:23；10-08 全表
  `created_at` 只有 09:53 / 15:00 两个瞬时。凡以 `created_at` 判新旧的监控与人读
  一律会误判为「早已停更」。
- **批量历史行大多无 `signal_ts`**（全表约 0.6% 的行有值）：任何「时间戳游标」
  增量同步本表都会**漏更新且不报错**——对外数据面已因此刻意排除本表
  （`backend/services/api/routers/external/datasets.py` 头部注释，勿当遗漏）。
- **读侧 `created_at DESC` 是刻意约定，勿改**：「每标的取最新一条 = 最后到达者
  胜」（`backend/shared/signal_scores.py`、`stock_lookback.py`、`stock_terminal.py`，
  后者带 2026-09-14 实测注释）；改成 `signal_ts` 排序会让批量行（大多无值）全部
  沉底，生产读数直接换口径。
- **容器时钟**：引擎容器时钟比宿主慢 1h（实测）；用 `now()` 的判读以宿主机时间为准。
- `trade_date` 是交易日（date）；跨日观察用 `source='realtime'` 过滤。

## 4. 表注释 DDL（可重放——新环境/重建库执行一次）

```sql
COMMENT ON TABLE engine_signal_scores IS
  '引擎信号分（批量+实时同表）。写入走 upsert（realtime_contract.mark_signal_ready / inference.script_runner）：DO UPDATE 只刷 signal_ts、不刷 created_at——created_at=首次落库时刻（实时行为当日首次入热集），signal_ts=真实最后写入时刻。判「写侧是否活跃」用 max(signal_ts)（source=realtime 行）或 engine_feature_runs.updated_at，勿用 created_at（假停更，审计 M1）。读侧「每标的取最新一条」刻意按 created_at DESC（最后到达者胜）——批量历史行大多无 signal_ts，勿改为 signal_ts 排序。巡检模板见 docs/engine-signal-scores-runbook.md';
COMMENT ON COLUMN engine_signal_scores.created_at IS
  '首次落库时刻（upsert 不更新，≠最后写入时刻；判写活跃请用 signal_ts）。审计 M1 / T4-3';
COMMENT ON COLUMN engine_signal_scores.signal_ts IS
  '信号最后写入时刻（实时行=本周期发布时刻，约每 15s 刷新；批量=写入时刻；历史批量行多为 NULL）。判写活跃的主判据';
```

> 备注：本部署已实库执行上述 COMMENT；并入版本化升级文件（`data/upgrade_*.sql`）
> 留待下次发版切版本时统一带出（避免回写已发布的版本文件）。

## 5. 关系

- 整改方案：`docs/实盘全天链整改方案_20261010.md` → T4-3（审计 M1）。
- 本 runbook 只固化「判活跃」口径与 COMMENT 重放；**不改**读侧 `created_at DESC`
  既有约定（有实测背书）。
