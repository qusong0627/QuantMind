---
name: model-inference-backfill
description: "模型推理入口导航与推理补全 — 一页找到推理链路（前端/HTTP API/Celery 自动推理/内部 runner），诊断推理覆盖缺口，批量把 pred.parquet 与 engine_signal_scores 补到指定交易日。触发词：推理入口、补推理、推理缺口、推理覆盖、inference backfill、模型推理到几号、补齐 pred、批量推理、推理到 10 月"
---

# 模型推理：入口导航 + 缺口补全

> 目的：下次要「补推理 / 查推理到哪天了 / 改推理行为」时，不用再翻代码。
> 全链路只有一个写入口：`InferenceScriptRunner.execute()`；覆盖缺口只看
> 模型目录里的 `pred.parquet`。

## 推理侧 Target Contract（2026-11-24 起）

- 模型目录 `metadata.json` 新增 `target_id/target_version/target_hash/target_base_hash/target_overrides/target_spec/label_contract_version/label_stats/contract_flags`；推理/回填只读模型目录，不重算训练目标。
- 分类模型推理输出正类概率（LightGBM/XGBoost 的 binary objective `predict`、CatBoost `predict_proba[:,1]`、sklearn `predict_proba[:,1]`）；DL 分类训练已被禁止，不要按分类模型消费 `.pth`。
- 成绩单、横评、组合回测按 `target_hash` 分组：**不同 target_hash 的模型不可合并比较**（持有期/标签口径可能不同）；没有该字段的老模型按 `target_id=legacy` + 合成 hash 处理。
- `target_mode` / `target_horizon_days` 仍保留在 metadata 供旧推理模板读取；新代码优先用 `target_id/target_hash/target_spec` 追溯口径。
- 若推理结果异常，先核对 `metadata.label_contract_version` 与 `contract_flags`（`capability_gate` / `diagnostic_gate`），再查 `label_stats` 的 NaN/正类率/每日截面。

说明：`docs/训练目标契约_使用说明_20261124.md`。

## 0. 30 秒速查

| 我要… | 去这里 |
|------|--------|
| 前端看/点推理 | 模型管理 → 模型详情 → 「推理覆盖」面板（`InferenceCoveragePanel.tsx`） |
| 单模型补到最新 | `POST /api/v1/models/{model_id}/inference/backfill`（按模型一次合并 pred；2026-11-28 起单模型内 4 日并发） |
| 查缺口到哪天 | `GET /api/v1/models/{model_id}/inference/coverage` |
| 多模型补推（前端） | 模型治理 → 勾选模型 →「批量补推」→ `POST /api/v1/models/inference/backfill-jobs`（同一套 `run_one_model`） |
| 多模型批量补（CLI） | `skills/model-inference-backfill/scripts/backfill_inference.py`（本技能） |
| 多日批量推理 | `POST /api/v1/models/inference/batch`（**不写 pred.parquet**，见 §4） |
| 每日自动推理 | Celery beat `engine.tasks.auto_inference_if_needed`（工作日 08:00） |
| 改推理代码 | `backend/services/engine/inference/script_runner.py` / `model_backfill.py` |

## 1. 写链路全景（唯一路径）

```
model_dir/inference.py --date YYYY-MM-DD --output <json>
        │  （InferenceScriptRunner 以子进程执行，超时 INFERENCE_SCRIPT_TIMEOUT_SEC，默认 3600s）
        ▼
InferenceScriptRunner.execute(date, tenant_id, user_id, persist=True)
        │
        ├─► engine_signal_scores      feature_version = script_v1_<模型桶>
        ├─► engine_feature_runs       特征运行台账
        └─► qm_model_inference_runs   运行记录（ON CONFLICT 幂等）
        ▼
merge_signals_into_pred(pred.parquet, signals_by_date)
        │
        ├─► <model_dir>/pred.parquet            全量重写（原子 temp + os.replace）
        └─► <model_dir>/pred_daily/dt=YYYYMMDD/data.parquet
```

关键事实：

- **`pred.parquet` 才是覆盖缺口的判定源**，不是 `engine_signal_scores`。
- 同一天重跑是幂等的：`_persist_locked` 先 DELETE 同
  `feature_version + model_version='inference_script'` 的旧行再插入；pred 合并按
  `(symbol, trade_date)` 去重保留最后一条。
- 模型隔离靠桶名：`feature_version = script_v1_<normalize_model_bucket(model_id)>`
  （小写、非字母数字折叠为 `_`、截断 48 字符，唯一实现在
  `backend/shared/signal_buckets.py`）。**不要手抄这个规则**，读写两侧必须共用。

## 2. 入口清单

### 2.1 前端（Electron）

| 文件 | 作用 |
|------|------|
| `electron/src/pages/InferenceCoveragePanel.tsx` | 单模型覆盖面板：查覆盖 + 一键补全 |
| `electron/src/components/inference/BatchInferencePanel.tsx` | 多日批量推理面板 |
| `electron/src/components/inference/BatchSingleDayPanel.tsx` | 单日推理 |
| `electron/src/services/modelTrainingService.ts:791-1205` | 所有推理 API 的前端封装 |

### 2.2 HTTP API（前缀 `/api/v1/models`，JWT，用户态）

挂载点：`backend/services/api/main.py:502`
实现：`backend/services/api/routers/model_training.py`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/inference/run` | 单日推理（body 带 model_id/date） |
| POST | `/inference/batch` | 多日批量编排（202；**不合并 pred**） |
| GET | `/inference/batches` | 批量历史 |
| GET | `/inference/batch/{batch_id}` | 批量进度 |
| GET | `/inference/batch/{batch_id}/aggregate` | 跨日聚合统计 |
| GET | `/inference/runs` / `/inference/runs/{run_id}` | 运行历史 / 明细 |
| GET | `/inference/latest` | 当前生效推理批次 |
| GET | `/inference/precheck` | 推理前置检查 |
| GET | `/{model_id}/inference/coverage` | **覆盖缺口**（min/max/gap_dates/data_cutoff_date） |
| POST | `/{model_id}/inference/backfill` | **单模型补全**（2026-11-28 起：单模型内 4 日并发 + 全缺口日一次合并 pred） |
| POST | `/inference/backfill-jobs` | **多模型补推**（模型间串行，单模型内 4 日并发；模型治理「批量补推」入口） |
| GET | `/inference/backfill-jobs` / `/{job_id}` | 补推任务列表/进度 |
| POST | `/inference/backfill-jobs/{job_id}/cancel` | 取消补推（当前模型完成后停） |
| GET | `/{model_id}/inference/backfill/{task_id}` | 补全任务状态 |
| GET/PUT | `/inference/settings/{model_id}` | 自动推理开关 / 每日执行时间 HH:MM |

### 2.3 后台自动推理

- Celery beat：`engine.tasks.auto_inference_if_needed`，工作日 08:00
  （`backend/services/engine/qlib_app/celery_config.py`，需 `AUTO_INFERENCE_ENABLED`）。
- 读 `qm_model_inference_settings`，按租户/用户/策略派发；走
  `router_service.run_daily_inference_script()` —— 该路径**会**合并 pred.parquet。
- 另有 `engine.tasks.backfill_inference_quality`（每日 02:30，滞后 5 天算生产 Rank IC）。

### 2.4 内部代码（脚本/任务直接调用）

| 模块 | 职责 |
|------|------|
| `backend/services/engine/inference/script_runner.py` | `InferenceScriptRunner`：执行脚本 + 落库 |
| `backend/services/engine/inference/pred_merge.py` | `merge_signals_into_pred`：回写 pred.parquet + pred_daily |
| `backend/services/engine/inference/router_service.py` | 每日自动推理入口（含 pred 合并） |
| `backend/services/engine/inference/batch_orchestrator.py` | 批量编排器（并发 5，最多 300 交易日 / 窗口 60 天） |
| `backend/shared/inference_coverage.py` | `find_inference_gap_dates`：缺口算法 |
| `backend/shared/signal_buckets.py` | 桶名唯一实现 |
| `backend/shared/inference_lock.py` | 推理锁（防并发同模型） |

> 旧 `batch_predict.py`（`data/predictions/*.csv`）只是旁路 CSV，**不落库、不写
> pred**，不要用它补数据。

## 3. 覆盖 / 缺口口径

- **A 集**：`engine_signal_scores` —— `trade_date` 是**生效信号日（T+1）**，symbol 为
  纯数字。
- **B 集**：`<model_dir>/pred.parquet` —— `trade_date` 是**数据日（T）**，symbol 为
  前缀式（`SH600036`）。coverage 与个股分数曲线读它。
- 缺口 = 从 `pred.parquet` 最小日期起、到**数据截止日**之间的交易日里，pred 没有的日期。
- **数据截止日**：`_quantdb_latest_factor_date()` 读 `l1_factors`/`l2_factors` 最大日期
  （T+1 更新）；2026-10-20 当时为 **2026-10-19**。缺口不会越过它。
- 交易日历用 `exchange_calendars` 的 `XSHG`。
- 缺失 ≠ 0：没有 pred 行就是没推理过，绝不当 0 分处理。

## 4. 三种补全方式怎么选

| 方式 | 写 pred.parquet | 并发 | 适用 |
|------|:---:|------|------|
| `POST /{model_id}/inference/backfill` / `POST /inference/backfill-jobs` | ✅ **每模型一次合并** | 模型间串行；**单模型内 4 日并发**（`BACKFILL_DAY_WORKERS` 默认 4） | 页面点「一键补全」、模型治理「批量补推」 |
| `POST /inference/batch` | ❌ **只写 signal_scores** | 编排器 5 | 多日批量看信号，不关心 pred 曲线 |
| CLI `backfill_inference.py` | ✅ 批量合并 | 模型间并发 N，合并串行 | **多模型 × 多周期批量补齐**（本技能） |

⚠️ 用 batch 编排器补历史后，coverage 仍显示缺口——因为它不写 pred。这是最容易
踩的坑。

### 4.1 2026-11-28 快速补推实现（当前线上逻辑）

- 核心：`backend/services/engine/inference/model_backfill.py::run_one_model`
  - 同一模型的全部缺口日先并发推理（默认 4，`BACKFILL_DAY_WORKERS` 可覆盖），信号收集完；
  - **最后只调一次 `merge_signals_into_pred`**（该函数本身支持 `[(day, signals), ...]` 多日一次合并），把旧实现「每天读 5.8M 行 pred 再整份写回」的 273 次全量读写压到 13 次。
  - 模型之间仍串行（`run_serial`），单用户同时只有一个补推任务。
- 补推期间必须 `ENABLE_TDX_PUSH=false`：否则 `InferenceScriptRunner` 会推送通达信选股并执行滚动买卖；批量补历史绝不能下真单/触发滚动，桥不可达时还会每天等超时。`backfill_all13.py` 已在脚本入口强制关闭。
- **代码改动后必须重启 API 容器**：`model_backfill` 在 API 启动时 import；网页/API 触发的补推跑在 API 进程里，不重启就仍用旧模块。独立 `docker exec` 脚本是新进程，不受影响。
- 进度语义：单日事件 `merged_rows=0`（合并推迟到模型结束），模型收尾时 `days_merged` 才更新；前端进度会按 4 天一批往前跳。
- 验证：日志出现 `[backfill-fast] ... workers=4`；任务行 `qm_model_inference_jobs` 的 `completed_days/total_days`；完成后每个子模型 `pred.parquet` 的 `max(trade_date)` 到最新交易日。


## 5. CLI 驱动用法

脚本：`skills/model-inference-backfill/scripts/backfill_inference.py`
（572 行，复用官方 runner + pred_merge，不另写推理逻辑）

### 5.1 前置

脚本依赖 `backend` 包与 pandas/duckdb/sqlalchemy/exchange_calendars，**必须在
quantmind 容器内跑**（cwd=/app）：

```bash
docker cp skills/model-inference-backfill/scripts/backfill_inference.py quantmind:/tmp/
```

### 5.2 先看缺口（不动手）

```bash
docker exec -w /app quantmind python /tmp/backfill_inference.py \
  --horizons 5,10,20 --end 2026-10-19 --created-after 2026-10-17 --dry-run
```

`--dry-run` 打印每个模型的 `covered_max / 待补天数 / 总天数`，不写任何数据。

> 模型筛选默认 `status='ready'` + `created_at >= --created-after`。实验模型多时，
> 用 `--model-id`（可重复）或 `--model-id-file` 显式白名单，避免误补 35 个模型。
> 排除正在被 API 补全的模型用 `--exclude-model-id`。

### 5.3 正式跑（推荐参数）

```bash
docker exec -d quantmind bash -c 'cd /app && exec python /tmp/backfill_inference.py \
  --model-id-file /tmp/model_ids_30.txt \
  --horizons 5,10,20 --end 2026-10-19 \
  --concurrency 3 \
  --status-file /tmp/inference_backfill_status.json \
  --report-file /tmp/inference_backfill_report.json \
  > /tmp/backfill_inference.log 2>&1'
```

### 5.4 监控

```bash
docker exec -i quantmind python - <<'EOF'
import json
s=json.load(open('/tmp/inference_backfill_status.json'))
tot=sum(m['total'] for m in s['models'].values()); done=sum(m['done'] for m in s['models'].values())
print(s['updated_at'], done, '/', tot,
      'ok', sum(m['ok'] for m in s['models'].values()),
      'fail', sum(m['failed'] for m in s['models'].values()))
for mid,m in s['models'].items():
    if m['done']: print(m['horizon'], m['model_type'], m['done'], '/', m['total'],
                         'merged', m['merged_rows'], m['current_date'])
EOF
tail -20 /tmp/backfill_inference.log
```

### 5.5 参数速查

| 参数 | 默认 | 说明 |
|------|------|------|
| `--end` | 必填 | 右界交易日 |
| `--start` | 无 | 左界；与「pred 最大日期次日」取较晚者 |
| `--scan-holes` | 关 | 同时回补训练期历史空洞（从 pred 最小日期扫起） |
| `--horizons` | `5,10,20` | 按 `metadata.target_horizon_days` 过滤 |
| `--model-id` / `--model-id-file` | 无 | 显式模型白名单 |
| `--exclude-model-id` | 无 | 排除（防与 API 补全双写） |
| `--created-after` | `2026-10-17` | 用户模型创建时间下界 |
| `--concurrency` | 3 | 模型级并发（内存瓶颈，别超 4） |
| `--merge-every` | 10 | 每 N 天合并一次 pred（减少全量重写次数） |
| `--longest-first` / `--no-longest-first` | 开 | 缺口最长的模型先跑（深模慢，先开工） |
| `--respect-data-cutoff` | 开 | 自动把 `--end` 收敛到因子数据截止日 |
| `--status-file` / `--report-file` | `/tmp/...` | 运维观测面 |
| `--dry-run` | 关 | 只打印计划 |

## 6. 硬约束（踩过坑的）

1. **补历史必须 `enable_fallback=False`**。`InferenceScriptRunner` 默认开兜底，
   兜底模型（alpha158）的分数写进该模型 pred 就是「别的模型冒充」，coverage 假性
   消除。CLI 驱动已固定关闭；自己写脚本时别忘了。
2. **不要与 API 补全同时写同一模型**。两个写者同时全量重写 pred.parquet，后写
   覆盖先写。启动前查：
   ```bash
   docker exec quantmind ps aux | grep 'inference.py --date' | grep -v grep
   ```
   有正在跑的模型就 `--exclude-model-id` 跳过，等它跑完再补剩余缺口。
3. **pred.parquet 全量读改写占内存**（千万行级，峰值数 GB）。`run_one_model` 现在同一模型只合并一次；旧的逐日路径仍是每天读改写。同一模型的合并必须串行（`inference_lock`/文件锁），不同模型可并发。
4. **深模慢**：树/线性约 11-12s/日；GRU/LSTM/ALSTM/TCN/Transformer/TabNet/NativeTFT 约 34s+/日。API 的 `run_one_model` 默认 4 日并发（`BACKFILL_DAY_WORKERS=4`），13 模型 × ~20 日约 20-40 分钟量级；设 1 可退回逐日。
5. **补推禁止实盘副作用**：`ENABLE_TDX_PUSH` 必须在补推进程里设为 false，否则每日推理后会尝试通达信选股推送 + 滚动买卖；批量补历史绝不允许下真单，桥不可达时还会每天等超时。
6. **代码改动后重启 API 容器**：`model_backfill` 在 API 启动时 import；网页/API 触发的补推跑在 API 进程里，不重启仍用旧模块。独立 `docker exec` 脚本是新进程，不受影响。重启后日志应出现 `[backfill-fast] ... workers=4`。
7. **状态文件写失败不能杀线程**：多线程共用 `.tmp` 会 `os.replace` 失败，CLI 已
   改成 pid+thread 唯一临时名并吞异常。
8. **服务端推理锁**：同模型同日并发会被 `inference_lock` 拦（`LOCK_HELD`），批量
   时不要对同一模型开两个驱动。
9. `pred.parquet` 新补的行 `label=NaN, split="test"` —— 收益标签要等时间兑现，
   分析时别把 NaN 标签当 0。

## 7. 验证补全结果

```bash
# ① 每模型 pred 最大日期 + 缺口
docker exec -i quantmind python - <<'EOF'
import duckdb, glob, os
for p in sorted(glob.glob('/app/models/users/default/*/*/pred.parquet')):
    d = duckdb.sql(f"SELECT min(trade_date), max(trade_date), count(DISTINCT trade_date) FROM read_parquet('{p}')").fetchone()
    print(os.path.basename(os.path.dirname(p))[-8:], d)
EOF

# ② signal_scores 落库抽查（按桶）
docker exec quantmind-db psql -U quantmind -d quantmind -c "
SELECT feature_version, count(*), min(trade_date), max(trade_date)
FROM engine_signal_scores
WHERE trade_date >= '2026-08-01'
GROUP BY feature_version ORDER BY 1;"
```

正常终态：每个模型 `max(trade_date)=2026-10-19`，且 `count(DISTINCT trade_date)`
与 coverage 面板一致（无 gap_dates）。

## 8. 排障

| 症状 | 原因 / 处理 |
|------|------|
| coverage 一直有缺口，但 signal_scores 有行 | 走了 batch 编排器（不写 pred）；改用 backfill / CLI |
| 网页/API 点补推仍然很慢，或进度一天一天走 | API 容器没重启，进程里还是旧 `model_backfill`；重启 `quantmind`/celery，日志确认 `[backfill-fast] ... workers=4` |
| 补推期间出现通达信推送/滚动买卖/桥超时 | 补推进程必须 `ENABLE_TDX_PUSH=false`；批量补历史不得触发实盘 |
| 进度一次跳 4 天、单日事件 `merged_rows=0` | 正常：4 日并发 + 合并推迟到模型结束；`days_merged` 在模型收尾更新 |
| `BACKFILL_DAY_WORKERS` 设了不生效 | 确认环境变量在 **job 进程**（API 容器/启动脚本）里；默认已是 4，设 1 可退回逐日 |
| 补完分数分布明显偏（全市场同一档） | 兜底模型污染；确认 `enable_fallback=False`，删掉该日 pred 行重跑 |
| `LOCK_HELD` | 同模型已有推理在跑；等锁释放或换模型 |
| 子进程超时 | `INFERENCE_SCRIPT_TIMEOUT_SEC` 默认 3600s；深模长窗口可调大 |
| 合并后 pred 行数变少 | 合并按 `(symbol, trade_date)` 去重；检查推理输出 symbol 是否被标准化成前缀式 |
| 日志无输出 | `docker exec` 少写 `-i`（heredoc 需要） |
| 模型没有 pred 历史 | CLI 会跳过并告警（不伪造历史序列）；需先有至少一天 pred 或手动指定左界 |

## 9. 相关技能

- `skills/batch-inference-analysis/` —— 补完之后**分析**推理结果（信号解读、行业
  轮动、选股）。
- `skills/model-train-infer-backtest-report/` —— 训练→推理→回测→报告的全流程。
