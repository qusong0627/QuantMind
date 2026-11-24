---
name: factor-select-train
description: "因子选型 → 跨库训练 → 回读验收 → 监控全链：从筛选 kept 名单里按「近期截面 IC + 语义分类均衡 + 冗余终检」选 N 个因子（默认 300），组装跨库 T+N 训练载荷（锚库+副库各自 pin 已发布目录版本、训练端不做二次筛选），提交后回读 config.yaml 断言、监控到完成并回报样本外指标（by_split rank_ic/ICIR、long_short、drift、产物与注册模型）。用户说「选因子训练」「训练一个 T+5 模型」「跨库训练」「提交训练任务」「拿筛选的因子训模型」时使用。触发词：选因子、训练模型、跨库训练、提交训练、T+5 训练、300 因子。"
---

# factor-select-train — 选因子 → 跨库训练 → 监控

把「筛选 kept 名单」变成「一批选好的因子 + 一个已提交且回读断言通过的跨库训练任务」，再把完成后的样本外指标带回来。**判据是 config.yaml 回读，不是 HTTP 200。**

全链四件套（缺一不可）：
1. **侦查**：候选因子源、目标 horizon、模型类型、训练窗口与内存预算（先看，别乱猜）
2. **选型**：`backend/scripts/select_training_features.py` → `training_selection_300.json` + 人读报告
3. **提交**：`backend/scripts/submit_crosslib_training.py`（先 `--dry-run`）→ 回读 config.yaml 断言
4. **监控/验收**：轮询 run 状态 → 完成后读 `eval_report.json` / `result.json` → 回报指标与产物

## Step 0 · 侦查（必做）

- 候选来源：`data/quantdb/factor_research/screening/`（`factor_selection.json` = 筛选 kept 名单，含 library/ic_mean/icir/turnover；`cross_corr.npz` = 池化相关矩阵）。
- 「可训池」= kept − `features_daily`（读层硬拦）− `factor_research`（不在 `6_ml_datasets` 体系），排除逐条记录在选型 JSON 的 `excluded_non_trainable`。
- **库版本现读，不硬编码**：`GET /api/v1/models/training-sources?market=CN` 给各库 `published/catalog_version`；提交脚本会逐库拉 `feature-catalog` 再校验。版本 map 可能随物化刷新而变。
- **窗口与内存**：全窗（2020 起）组装单帧约 9G 会 OOM；当前口径是 train 起点回退到 **2022-10-17**。读取端按 split 分段取（自动前后各扩约 8 天留标签窗）。提交前 `free -g` 看宿主 available（本轮 32G 起步 → 读取期掉到 16G 仍安全）。
- 训练串行、无回收：一次只跑一个训练容器（`qm-train-<run_id>`），失败不自动重试。

## Step 1 · 选型

```bash
python3 backend/scripts/select_training_features.py                 # 全量选型（默认 300 / T+5）
python3 backend/scripts/select_training_features.py --max-dates 12  # 冒烟：只算最近 12 期
python3 backend/scripts/select_training_features.py --n-target 200 --horizon 10 --corr-threshold 0.8
```

口径（脚本 docstring 为准）：近窗截面 IC 与训练**逐字同口径**（锚库 close 剔 volume==0 填充行 → shift lag=1/horizon=5 → label(t)=close(t+6)/close(t+1)−1；主窗默认 2025-09-01..2026-09-10，对照窗 = 主窗末 −6 个月）→ 门槛 `--min-dates 120` / `|ic_12m| ≥ --min-abs-ic 0.01` → √n 配额分类均衡（≤15 条的类别全取）→ |ρ|≥0.85 贪婪去冗余 + 同类顺位替补。

产物（`data/quantdb/factor_research/screening/`）：
- `recent_ic.parquet`（逐日 IC 时序，可审计）
- `training_selection_300.json`（**唯一事实源**：`train_plan` + `features_payload` + 配额表 + 排除/冗余明细）
- `训练选型报告_YYYYMMDD.md`（先给用户审阅漏斗/配额/各类 Top，再提交）

## Target Contract（2026-11-24 起）

- `train_plan.label` 可直接带 `target_ref: {id, version}` 与 `label_overrides`；`submit_crosslib_training.py` 会透传并用 `expected_target_hash`（若提供）校验复现。
- 源计划带 `target_ref` 时改周期：改 `label.label_overrides={"horizon_days": N}`，不要只改 `label.target_horizon_days`（会被冻结 spec 覆盖）；新训练删除 `expected_target_hash`。
- 源计划无 `target_ref`：继续 legacy 平铺字段，行为不变。
- 回读断言新增：`label.target_id` / `label.target_version`（若计划带 ref）必须与计划一致；config 缺失 `target_id` 而计划带 ref → 没交付。
- 能力硬闸：classification 不支持 7 个 DL；quantile 仅 CN 单 LightGBM；排序损失仅三棵 GBDT；`size_excess_*` 需 `total_cap`；`execution_price=open` 需直读因子源。完整矩阵见 `docs/训练目标契约_使用说明_20261124.md`。

## Step 2 · 提交（跨库）

`train_plan` 里的 `job_name/display_name/model_type/split/label/context/num_boost_round/early_stopping_rounds/max_time_minutes` 按本轮需求改（默认 = T5_Cross300_LGB / lightgbm / 2000 轮 / 早停 100 / 720 分钟）。

```bash
QM_PASSWORD=... python3 backend/scripts/submit_crosslib_training.py --dry-run   # 先看载荷
QM_PASSWORD=... python3 backend/scripts/submit_crosslib_training.py             # 正式提交
```

**对比训练（同因子/窗口换模型类型）**：`--model-type xgboost|catboost|lightgbm` 在命令行覆盖，job/display 名自动按 token 改写（LGB↔XGB↔CAT）；**必须同时 `--pin-versions-from <基准 run 的 config.yaml>`**——逐库目录版本与基准不一致即拒绝提交（保证三只模型吃同一份因子快照）。串行跑：一只完成再提交下一只。

```bash
QM_PASSWORD=... python3 backend/scripts/submit_crosslib_training.py \
    --model-type xgboost \
    --pin-versions-from data/training_jobs/train_20261016083816_f668beed/config.yaml
```

脚本自带回读断言（轮询 `data/training_jobs/<run_id>/config.yaml`，默认 180s）：特征数=300、`factor_field_sources` 逐条来源一致、`label.target_horizon_days`、`factor_selection.method == ""`、版本 map 逐字一致、切分窗口逐字一致、`required_artifacts` 六件。**任何一条不过就是没交付。**

### 跨库契约（逐字认准，改载荷前先读）

- `factor_source` = 锚库；`factor_catalog_version` = 锚库版本（锚库必填）；`factor_catalog_versions` **只放副库**（anchor 同库条目会被忽略）。
- `features`：锚库裸名，或 `"库:列"`（**输出列名 = 裸列名**）；输出列名跨库必须唯一，重名 → 422；未启用特征 → 422。
- 训练端不做特征筛选 = `factor_selection.method=""`（pfs/dh 的开关状态不是闸门，**method 才是**）+ 载荷白名单键 `lgb_params`（非 lightgbm_params）、`pause_others=false`。
- 产出对照表 `factor_field_sources`：副库 `"库:列"`、锚库裸名。

## Step 3 · 监控与验收

```bash
# 状态（3s 轮询端点，含 logs/result）
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/models/training-runs/<run_id>
# 该任务提交时的真实载荷（供前端恢复态「请求预览」核对；~24KB 别放进轮询）
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/models/training-runs/<run_id>/request
```

- 运行期日志看「Memory-optimized read: selected N columns」「Direct QuantDB read ...」到「Training finished in Ns」；`[REAPER] Reattached container ...` 是编排器在看门狗/热加载后重挂，属正常。
- **完成后读两个文件**（`data/training_jobs/<run_id>/`）：
  - `eval_report.json`：`rank_ic`（mean/icir/win_rate/t_stat）、`by_split.{train,valid,test}`、`long_short`（ann_return/sharpe/max_dd）、`yearly`、`ic_curve`
  - `result.json`：`metrics.{train,val,test}.{rmse,auc}`、`artifacts`、`model_registration`（model_id/status/storage_path）、`drift`（stable/medium/severe + max_psi + 噪声底 + 近窗区间）
- 注册模型落盘：`models/users/<tenant>/<user_id>/<model_id>/`（宿主路径；容器内 `/app/models/...`），8 件产物（含 `eval_report.json`/`shap_summary.csv`）。
- 训练页（`#/model-training`）监控台：恢复进行中任务时「请求预览」显示**任务真实载荷**并带「任务载荷」标记——若显示 0 特征，先查是不是恢复了任务而草稿为空（已修复：`GET .../request` 端点 + `runRequest` prop）。
- **回报口径**：样本外指标优先 `by_split.test / by_split.valid`，别用全窗 headline 冒充样本外（全窗虚高约 1.5×）；对异常强的段（如 test 56 天 win_rate 96%）要明说是单一行情；缺失数据一律「—」绝不为 0。
- **横评/自查**：`python3 backend/scripts/compare_training_runs.py [--vs-archive] <run_id>...` —— 一张 markdown 表拉齐 by_split/rmse/auc/long_short/drift/逐年，`--vs-archive` 附全部历史 run 的分位排名（单看绝对值分不清「模型好」和「这行情大家都好」）。

## 铁律 / 已知坑

- 训练串行 + 无回收；`pause_others=false`，不动并行会话的容器。
- api 热加载（改了后端代码让 api 拾新码）：**只 kill 8000 端口持有者**（`/proc/net/tcp` 0x1F40 反查），禁 `docker restart quantmind`、禁按进程名杀；**交易时段 09:30-11:30/13:00-15:00 CST（按 `docker exec quantmind date`）禁热加载**。
- 判「提交成功」看 config.yaml 回读，不看 HTTP 200；判「任务成败」看 run 终态，不按有无异常。
- 展示面（研究评分卡/信号卡）只说位置词，不写「前/后 N%」（`signal_side` 有两条生成路径）；UI 禁内部工单号；模型展示名用中文。
- 换窗口/换 model_type 后**重新走一遍回读断言**；`training_selection_300.json` 是唯一事实源，别手改 features 数组——改需求就重跑选型或改 `train_plan` 后重新提交。
- 相关：`docs/因子选型与T5跨库训练_细案_20261016.md`（细案）；训练内存教训见记忆 training-memory-budget；热加载见 oss-backend-hot-reload。
