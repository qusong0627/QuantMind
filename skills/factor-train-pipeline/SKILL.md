---
name: factor-train-pipeline
description: "因子训练链路：把「因子研究」筛选保留集（可按库剔除，如去 L2）合并为自定义市场数据集 → 发布训练目录 → 提交生产级模型训练（LightGBM 等，防泄露口径：按年切分+embargo+截面预处理）→ 监控进度 → 读取指标；含每日 03:00 自动重建调度与 CUSTOM→CN 模型市场迁移。触发词：因子训练、筛选因子拿去训练、去L2训练、273因子、合并因子训练、自定义市场训练、训练LightGBM、训练因子模型、自定义数据集重建、模型迁到A股"
---

> ## ⚙️ 运行环境契约
>
> 凡 import `pandas/duckdb/backend` 等重依赖的脚本，**必须在 quantmind 容器内执行**：
> `docker cp <脚本> quantmind:/tmp/ && docker exec -w /app quantmind python3 /tmp/<脚本> <参数>`。
> API 一律 `http://127.0.0.1:8000`（宿主）或 `http://quantmind:8000`（容器网络），管理员令牌：
> `POST /api/v1/auth/login {"username":"admin","password":"admin123","tenant_id":"default"}`。

# 因子训练链路（筛选 → 自定义市场 → 生产级训练）

把「因子研究」里筛选好的因子（例如：七库联合筛选保留集，剔除 L2 后 273 个）合并成**一份
自定义市场数据集**，然后按生产级口径训练单模型（默认 LightGBM）。平台直读训练限定
**单数据源**，自定义市场（`/data/quantcustom`）是多库因子合并后一次训练的唯一官方通道。

## 流水线总览

```
① 选因子      screening 保留集 ──按库剔除──▶ 训练目录勾选发布（default_selected）
② 合并数据    build_factor_custom_dataset.py ─▶ /data/quantcustom/6_ml_datasets/l1_factors/dt=*
③ 注册目录    字段发现(CUSTOM) ──▶ 建草稿 ──▶ 播种全部字段 ──▶ 发布（拿 active 版本号）
④ 提交训练    POST /api/v1/models/run-training（载荷见 assets/train_payload_template.json）
⑤ 监控/结果   GET /api/v1/models/training-runs/{runId} 轮询；完成后读 train/val/test 指标
```

## ① 选因子（因子研究筛选集 → 训练勾选）

```bash
# 保留集在 <quantdb>/factor_research/screening/factor_selection.json
# 剔除 L2（或任意库）并导出清单 + 发布到训练目录勾选：
docker exec -w /app quantmind python3 backend/scripts/apply_factor_selection_to_catalog.py \
  --source kept --without-libraries l2_factors
# 只要 L1：--without-libraries l2_factors,alpha_library,alpha360,jq110,tdxgs
# 导出清单（构建数据集要用）：<quantdb>/factor_research/screening/kept_features_excl_<库>.txt
```
注意：`factor_research` 库的因子是研究专用（无 6_ml 目录），脚本会自动跳过。

## ② 合并数据集（多库因子 → 自定义市场 parquet）

```bash
docker exec -d quantmind bash -c 'cd /app && python3 backend/scripts/build_factor_custom_dataset.py \
  --start 2016-01-01 --min-coverage 0.65 > /tmp/custom273_build.log 2>&1'
# 跟踪：tail -1 /tmp/custom273_build.log（进度行）+ 完成标志 [5/5]
# 首次/全量 ~45-75 分钟（2599 分区，慢盘实测）；之后默认走增量，只补缺失分区（秒级）
```
增量语义（2026-09-15 起）：`meta.json` 记录筛选指纹 + 股票池 + start/min-coverage，
三者一致时只写缺失分区；筛选集重筛 / 参数变化 → 自动回退全量。`--full` 强制全量
（例：源数据前复权基准被回溯改写后，需要全量重写历史分区时）。
脚本默认值（DEFAULT_START=2016-01-01 / DEFAULT_MIN_COVERAGE=0.65）与每日调度共用，
改这两个常量后 **celery worker 要重启**（否则缓存旧值 → 每晚误判参数不一致而全量）。

口径（**与回测一致，防泄露友好**）：
- 股票池：全 A **非 ST/退市**；覆盖率阈值随窗口长度调整（全窗口口径）：6 年窗（2020 起）
  0.9 约留 3600 只；10.7 年窗（2016 起）0.9 只剩 2579 只（老票偏置），0.65 约留 3271 只，
  与短窗口径的推理覆盖基本持平，同时仍剔除交易不足窗口 70% 的长期停牌/次新票；
- **可交易性**：当日涨停或跌停 → `close` 置 NaN（标签不可算 = 样本剔除，买不进/卖不出不进样本）；
- 分区含**全套 OHLCV + date(YYYY-MM-DD)**（直读必需列），价格=前复权；
- 因子缺失写 NaN，由训练端截面预处理负责填充。

⚠️ 输出目录 = `QM_QUANTCUSTOM_DATA_DIR`（默认 `/data/quantcustom`）。**不要**用
`quantdb/quantcustom` 路径（reader 读的是 /data/quantcustom）；冒烟产物清理要在容器内删。

## ③ 注册自定义市场目录

```bash
TOKEN=...  # 见环境契约
# 字段发现（扫描本地 parquet 注册字段）
curl -s -X POST "http://127.0.0.1:8000/api/v1/admin/training-data/sources/refresh?market=CUSTOM" -H "Authorization: Bearer $TOKEN"
# 建草稿（返回 version_id）
curl -s -X POST "http://127.0.0.1:8000/api/v1/admin/training-data/versions?market=CUSTOM" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"source_dataset":"l1_factors","version_name":"CUSTOM 筛选保留"}'
```
然后把该数据集的全部字段播种进草稿并发布（SQL,容器内 psql）：
```sql
INSERT INTO qm_training_factor_mapping
  (mapping_id, version_id, source_dataset, source_column, feature_key, display_name,
   category_id, category_name, enabled, default_selected, required, sort_order)
SELECT 'seed-'||md5('l1_factors-'||column_name), '<VID>', 'l1_factors', column_name, column_name,
       column_name, 'other', '待分类', TRUE,
       column_name IN ('每个','选中','因子','名'), FALSE,
       row_number() OVER (ORDER BY column_name)
FROM (SELECT DISTINCT column_name FROM qm_quantdb_factor_field
      WHERE market='CUSTOM' AND dataset_id='l1_factors' AND is_present) t;
UPDATE qm_training_factor_catalog_version SET status='published', published_at=NOW() WHERE version_id='<VID>';
```
> 也可以让用户在后台「模型训练数据集 → 自定义市场」页用「字段发现 → 新建草稿 → 发布」完成本步
> （用户自己发布时，其版本会取代上面 SQL 建的版本成为活动版本——提交训练时以**活动版本**为准）。

## ④ 提交训练

载荷模板见 `assets/train_payload_template.json`（从一次成功工单导出）。**必改/必查清单**：

> 模板里 `system_notices` 已清空——该字段是**上一次提交的日期修正回显**（实测踩过：导出的旧载荷里通知
> 提到的日期与载荷自身字段不符），复用旧载荷时要清掉或重新核对，别把旧通知当真。

| 字段 | 值 | 说明 |
|---|---|---|
| `model_type` | `lightgbm` | 单模型 |
| `model_types` / `ensemble` | **必须删除** | 载荷里带 `model_types` 会一次训 13 个模型（含 DL，CPU 上极慢） |
| `train_start/end` | 例 `2016-01-04` ~ `2024-12-31` | 按年切分；训练含 embargo 自动丢边界 6 交易日 |
| `valid_start/end` | 例 `2025-01-02` ~ `2025-12-31` | 验证一年 |
| `test_start/end` | 例 `2026-01-02` ~ `2026-09-11` | 测试期 |
| `features` | 273 个因子名（一行一行的清单文件转数组） | ②导出的 `kept_features_excl_*.txt` |
| `auto_feature_filter` | `false` | **关闭过滤** = 不再做 IC/ICIR 筛选，直接用提交的 features |
| `preprocessing` | `{"enabled": true, "winsor": true}` | 截面中位数填充 + 1%/99% 缩尾 + Z-score |
| `factor_source` | `l1_factors` | 自定义市场的源目录名 |
| `context.market` | `CUSTOM` |  |
| `factor_catalog_version` | **当前 active published 版本号**（上一步） | 不是活动版本会 422；用户重发布后需重新取 |
| `lgb_params` | lr 0.05 / leaves 31 / depth 8 / min_data 50 / ff 0.7 / bagging 0.7 | 保守生产默认 |
| `num_boost_round` / `early_stopping_rounds` | 1000 / 100 | 早停自动截断 |

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/models/run-training" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data @/tmp/train_payload.json
# 返回 runId / validFeatureCount / missingFeatureCount —— valid 应等于特征数、missing 0
```

## ⑤ 监控与结果

```bash
RID=train_...
curl -s "http://127.0.0.1:8000/api/v1/models/training-runs/$RID" -H "Authorization: Bearer $TOKEN" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status'], d['progress']); print(d['logs'][-600:])"
docker logs qm-train-$RID --tail 20      # 容器直看（日志流更实时）
docker ps --filter name=qm-train         # 容器在 = 在跑；stats 看 CPU/内存
```
阶段里程碑：`Direct QuantDB read`（读数）→ `Built mom_ret_1d` → `Data ready` → PSI 漂移 →
`Split mode + Embargo` → 截面预处理 → `Training lightgbm` → `Training finished in ...s,
best_iteration=...`（完成后 metrics 写入 train/val/test，模型注册到模型列表）。

**结果判读**：三集合 RMSE/AUC 差距小 = 无过拟合、无样本外衰减；AUC>0.5 = 有预测力
（A 股全市场日频 0.52~0.53 即可用）。同期实测样例：273 因子 LightGBM（162 轮早停，
44 分钟）train/val/test AUC = 0.5284/0.5222/0.5260，RMSE 三集合一致。

## ⑥ 每日自动重建（同步调度）

自定义市场数据集是**派生数据**（源头是 A 股 QuantDB 的 daily_forward + 五个因子库），
已作为独立「市场」`CUSTOM` 挂进平台的市场同步调度（与 A/US/HK/BC/FUTURES 同一套机制）。

- **配置**：Redis `quantmind:sync_schedule:CUSTOM`，当前 = `{"enabled": true, "time": "03:00"}`，
  排在 A 股同步（00:55）之后等源数据落盘。前端入口：「数据管理 → A股」tab 的
  「自定义数据集重建」面板（开关 / 时间 / 立即重建一次）。
- **命令行**（查看配置 / 手动触发，等效前端按钮）：
  ```bash
  curl -s "http://127.0.0.1:8000/api/v1/admin/data-platform/sync-schedule/CUSTOM" -H "Authorization: Bearer $TOKEN"
  curl -s -X POST "http://127.0.0.1:8000/api/v1/admin/data-platform/sync-schedule/CUSTOM/run" -H "Authorization: Bearer $TOKEN"
  ```
- **执行体**：celery worker 调 `build_factor_custom_dataset.rebuild()`（增量模式），
  任务名 `engine.tasks.run_market_scheduled_sync`、队列 `qlib_backtest_srv`，同日去重
  （`quantmind:sync_schedule_last_run:CUSTOM:<date>`）。跟踪：`docker logs quantmind-celery | grep -E "保留集|待写|完成"`。
- **改调度器代码后**：worker/beat 需重启才拾取新代码
  （`docker restart quantmind-celery quantmind-celery-beat`）；API 侧改 admin 路由走
  「kill api 子进程（监听 8000）让看门狗 respawn」。
- **局限**：增量只补缺失分区——源数据前复权基准被回溯改写（除权除息）时历史分区不会
  自动重写，需要时手动 `--full`（全量 ~45-75 分钟，2599 分区）。

## 实战坑清单（全踩过）

1. **CUSTOM 市场三处接线**（缺一处就静默失败/422/无数据）：`request.resolve_market` 白名单、
   `local_docker_orchestrator._MARKET_DATA_MOUNT_DIRS/_MARKET_MOUNT_ENV_VARS`、
   容器内 `docker/training/data/loading.py:_MARKET_DATA_DIR_ENV`；
2. 分区 `date` 必须 `YYYY-MM-DD`（写 `20200102` 会在 DuckDB CAST 报 invalid date）；
3. 直读必需列 `symbol,date,open,high,low,close,volume,amount` —— 只给因子列不行；
4. 数据目录必须是 `/data/quantcustom`（`QM_QUANTCUSTOM_DATA_DIR`），不是 quantdb/quantcustom；
5. 载荷里的 `factor_catalog_version` 必须等于该 (market,source) 的**当前** published 版本
   —— 用户在后台重新发布过目录的话，先查 `qm_training_factor_catalog_version` 取最新；
6. 训练容器内存 = 宿主 80%（本机约 49G）超限直接 **ExitCode 137**，且**不落 result.json**；
   判别：容器 dir 无 result.json = OOM（有 result.json 才是业务错误）；
7. 大数据直读的三大内存坑（已修）：预处理逐格 `.loc`+整表 float64（已分块向量化）、
   加载器冗余 `.copy()`（已去）、池过大（用 `--min-coverage` 控）；峰值应稳在 ~20G；
8. `model_types` 不删 = 一次训 13 模型（lightgbm 先跑但结果要等全部跑完才注册）；
9. 提交时撞上正在重建的数据分区（构建未完成）→ DuckDB "don't know what type"；
   等 `[5/5] 完成` 再提交；
10. 冒烟/测试产物清理：`rm -rf` 要发生在**容器内**（宿主删不到 /data）；
11. 训练注册的市场跟随训练市场：`context.market=CUSTOM` 注册进**自定义市场**，而前端
    市场切换器只有 CN/HK/US/CRYPTO/FUTURES —— 不迁移的话模型在**整个前端都不可见**
    （只有后台「数据管理 → 模型扫描」接口能看到）。迁移用一条命令（改 metadata +
    搬目录 + 改 display_name 后缀 + 迁移溯源 + 就绪校验，幂等可重跑）：
    ```bash
    docker exec -w /app quantmind python backend/scripts/migrate_model_market.py \
      --model-id <mdl_...> --to-market CN        # 先加 --dry-run 预演
    ```
    273 样例模型已于 2026-09-15 迁 CN，A 股模型管理/推理中心即时可见；推理取数不受
    影响（仍从 metadata.quantdb_dir pin 的 /data/quantcustom 读，日历本就按 CN）；
12. 截面预处理的语义必须「先缩尾后统计」（统计量取缩尾后的数据），否则极值把 mean/std
    抬高、Z 分全部塌成常数（历史修复已在 preprocessing.py 固化并有回归脚本）。

## 验证 Checklist

- [ ] ② 完成：`ls /data/quantcustom/6_ml_datasets/l1_factors/ | wc -l` ≈ 2600（2599 分区 + meta，2016 起口径）
- [ ] ③ 完成：字段发现返回 files=分区数；published 版本含全部因子且 enabled
- [ ] ④ 提交返回 `validFeatureCount == len(features)` 且 `missing == 0`
- [ ] ⑤ 日志出现 `Split mode` + `Embargo` + `Training finished`；容器结束后 result.json 存在
- [ ] 完成后：模型管理能看到新模型（status=ready）；train/val/test 指标无断崖
- [ ] ⑥ 调度生效：`sync-schedule` 里 CUSTOM `enabled=true`（03:00）；次日自定义目录出现新交易日分区
- [ ] 模型可见性：CUSTOM 训练的模型已用 migrate_model_market.py 迁到目标市场（否则前端不可见）

## 参考实现

- 构建脚本 `backend/scripts/build_factor_custom_dataset.py`（rebuild() 供调度复用；增量 + `--full`）
- 勾选发布 `backend/scripts/apply_factor_selection_to_catalog.py --source kept`
- 市场迁移 `backend/scripts/migrate_model_market.py --model-id <id> --to-market CN`
- 调度挂载 `backend/services/engine/tasks/market_sync_scheduler.py`（MARKETS 里的 CUSTOM 分支）
- 载荷模板 `assets/train_payload_template.json`
- 成功样例工单：`train_20260914130341_887a7a0d`（273 因子 / 3600 只 / 2020-2024 训、2025 验、2026 测）
- 样例模型 `mdl_cust_train_20260914130341_887a7a0d_c2e90650`（已迁 CN，A 股模型管理可见）

> 免责声明同项目根 CLAUDE.md：仅供学习研究，不构成投资建议。