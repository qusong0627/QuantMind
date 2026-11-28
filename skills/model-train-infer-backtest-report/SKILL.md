---
name: model-train-infer-backtest-report
description: "模型训练-推理-组合回测-专业报告 全流程 — 提交T+N周期模型训练（13种模型类型：lightgbm/xgboost/catboost/random_forest/linear/mlp/gru/lstm/alstm/transformer/tabnet/tcn/nativetft，GPU训练+GPU推理）、批量推理全年、自定义组合策略回测(分数阈值+大盘MA+止损)、严格评估（净Top-N/三档成本/HAC/PSR）、数据审计（PIT/泄漏/可成交）、多种子、A/B对比、导出研报级MD+PDF报告。在 QuantBot / Claude Code 中训练模型、推理历史、回测策略、对比T+N周期、止损、大盘过滤、出报告、效益分析时使用。触发词：训练模型、模型训练、13种模型、推理全年、回测策略、T+3、T+5、周期对比、止损、大盘过滤、出报告、效益分析、严格评估、净Top20、数据审计、多种子、A/B对比"
---

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 本技能可能运行在 **QuantBot（QwenPaw 容器）** 或**宿主机/本地 Claude Code**。执行前先探测环境（`which docker`、API 连通性），并遵守以下映射规则：
>
> 1. **后端 API 地址**：QwenPaw / 容器网络内一律用 `http://quantmind:8000`（`quantmind` 是 docker 网络别名）；仅宿主机调试用 `http://127.0.0.1:8000`。正文中出现的 `127.0.0.1:8000`、`localhost:800x`，在 QwenPaw 环境下自动替换为 `http://quantmind:8000`。
> 2. **取数脚本执行**：凡 import 了 `pandas / duckdb / psycopg2 / numpy / sqlalchemy` 等重依赖或 `backend` 包的脚本，**必须在 quantmind 容器内执行**（QwenPaw 本地 venv 无这些依赖）：
>    ```bash
>    docker cp <脚本路径> quantmind:/tmp/<脚本名> && docker exec -w /app quantmind python3 /tmp/<脚本名> <参数>
>    ```
>    脚本源三选一：宿主机 repo `skills/<name>/scripts/`、QwenPaw 工作区 `/app/working/workspaces/default/skills/<name>/scripts/`、挂载目录 `/quantmind/skills/<name>/scripts/`。纯标准库脚本（无重依赖）可在 QwenPaw 本地直接跑。
> 3. **报告落盘**：股票报告页可见的 MD/PDF 报告，直接写 `/data/reports/trading_agents/{市场或类别}/{股票名}/`（QwenPaw 对 `/app/db` 有写权限，**直接写文件，不要 docker cp**）；过程数据 facts 写 `/data/reports/<类别>/`（`/data` 可写）。
> 4. **MD → PDF 转换（按优先级降级）**：
>    ① `docker exec -w /app quantmind python3 backend/scripts/md_to_pdf_report.py <输入.md> <输出.pdf>`（研报级排版，首选）；
>    ② docker 不可用时，**改用 QwenPaw 内置 `pdf` 技能**把 MD 转成 PDF；
>    ③ 两者都不可用则只交付 MD，并明确告知用户 PDF 未能生成及原因。
> 5. 本文中的 `~/.claude`、`cp -r ... ~/.claude/skills` 等说明仅适用于本地 Claude Code 维护者，**QuantBot 不要执行**。

# 模型训练-推理-回测-报告（全流程）技能

覆盖 QuantMind 量化策略的**完整闭环**：训练模型 → 严格评估/数据审计 → 推理历史 → 组合回测 → 专业报告。用于验证"改个参数（周期/阈值/风控）收益会不会更好"这类问题，用数据说话而不是猜。

## 架构总览

```
① 训练模型 (run-training API)
   ├─ 模型类型 (13 种全支持): lightgbm / xgboost / catboost / random_forest /
   |  linear / mlp / gru / lstm / alstm / transformer / tabnet / tcn / nativetft
   ├─ T+N 周期选择 (T+1/T+3/T+5...)
   ├─ 特征来源必须可解析：缺失即 422（allow_missing_features=true 才放行）
   ├─ 损失: regression / binary / rank_xendcg / lambdarank
   ├─ 样本卫生: tradeable_mask / ST PIT / 时间衰减
   ├─ 质量门禁: test_rank_icir<0.05 或 test_rank_ic 非正 → status=candidate
   └─ 训练完成 → 自动排队 严格评估 + 数据审计（应用侧，需重启后生效）
①.5 严格评估 / 数据审计 (后置产物)
   ├─ 严格评估: 净 Top-N × 三档成本 + HAC/PSR + 月度 + 换手 + 容量
   ├─ 数据审计: 未来列 / 标签契约 / trade_mask / ST / 幸存者 / 指纹 / PIT 探针
   └─ 晋升闸: 净 Top20(25bp)>0 且审计无 fail 才可 promote
② 推理历史 (批量推理 API)
   ├─ 单日 /models/inference/run
   └─ 批量 range 模式 推理全年逐个交易日
③ 组合回测 (自定义脚本)
   ├─ backtest_l2_top20.py      基本 Top20 每日滚动
   ├─ backtest_l2_year.py       全年严格版 (滑点+T+1+涨跌停+ST剔除)
   └─ backtest_l2_optimized.py  优化版 (分数阈值+大盘MA+止损5%)
④ 专业报告 (report_l2_optimized.py)
   └─ 回测数据 → 研报级 MD → md_to_pdf_report.py → PDF
```

## 2026-11-28 更新：网页训练全链路 + 快速补推（必读）

### A. 训练：优先走网页/API，不要直跑 train.py

- **网页/API 路径**（`POST /api/v1/models/run-training`）会创建 `qm_training_runs` 记录、保存完整 result/logs、训练完成后自动注册模型并拆分多算法条目；前端训练历史/结果页的图表依赖这条记录。
- **直跑 `docker exec ... train.py --config`** 只会产出工作区文件与 `result.json`，**没有 `qm_training_runs` 记录**，网页训练历史看不到，图表也会缺；仅用于临时实验。临时实验要图表口径一致，至少把 `cache.dir` 设为 `/data/training_cache` 并走注册脚本。
- **多模型串联（13 个一起）**：请求里用 `model.types: [lightgbm, xgboost, catboost, linear, random_forest, mlp, gru, lstm, alstm, transformer, tcn, tabnet, nativetft]`，`ensemble: none`；后端 `train_multi_models` 顺序训练、生成 `comparison`，按 `val_rank_icir` 选 primary，再 `split_multi_model_entries` 拆成 13 条单算法记录。
- **选优口径警告（30 特征 13 模型实测）**：primary=NativeTFT（val ICIR 0.7882），但 test RankIC 最高的是 CatBoost 0.0910 / Linear 0.0899 / XGBoost 0.0898，NativeTFT test RankIC 仅 0.0797。**不要把 primary 自动当默认模型**；晋级看 test 指标 + eval 中心闸门。

### B. 2026-11-27 机构级默认值（训练页新建草稿生效）

| 模型 | 默认 |
|---|---|
| LightGBM | lr 0.03 / leaves 31 / depth 6 / min_data 500 / bagging_freq 1 / L2 5.0 / rounds 1000 |
| XGBoost | depth 5 / subsample 0.8 / colsample 0.8 / reg_lambda 10 / min_child_weight 500 |
| CatBoost | depth 5 / l2 10 / random_strength 2.0 / bagging_temperature 1.0 / iterations 2000（独立下发） |
| GRU/LSTM/ALSTM | `dl_lr` 3e-4（旧 1e-3 在大表首轮过冲） |
| MLP | hidden 128 / lr 1e-3 / epochs 500（前端专属默认块） |

训练护栏（2026-11-27 起）：
- 特征名含 `label*` / `fwd_ret*` / `future_ret*` → 提交 422、训练入口 RuntimeError；`return_{n}d` 只告警。
- 切分时点完整性：train→valid / valid→test 交易日间隔必须 ≥ `horizon + 1`；短段无法裁 embargo 直接失败（旧实现只告警跳过）；WFA 窗口与 OOF 折也补了 `horizon + lag` embargo。
- LGB 对 `ind_code_l1/l2` 声明 categorical；LGB 早停 `first_metric_only=True`；TCN 感受野 < `step_len` 会告警并写 `dl_metadata.structure_guard`。
- **DL 预测帧已带评估列**（`label_return/OHLC/中性化控制列/label_return_h*`，2026-11-28 修）：批量训练主模型是 DL 时，`eval_report` 不再退化成 `label_rank`、`residual_ic` 不再为 None，前端图表与单模型网页训练一致。**旧模型/旧 DL 子模型的 pred 仍是 5 列，需要重训才有完整图表**。

### C. 训练数据读取：先看缓存，再谈 DuckDB

- 冷读实测：`l1_factors` 2021-12~2026-11，5,831,582 行 × 36 列 = **135.3s**；同特征/同窗口命中训练缓存 = **约 15s**。
- 平台 Docker 编排器已把宿主机 `data/training_cache` 挂到容器 `/training_cache`（跨任务复用）；**直跑 config 必须把 `cache.dir` 写成 `/data/training_cache`**，否则 `/tmp` 随 `--rm` 容器消失，每次都冷读。
- DuckDB 已是读取引擎；count 扫描 5.83M 行 4 线程 5.78s、16 线程 4.84s——**扫描不是瓶颈**，135s 主要花在 4,075 个日分区的列解码 + `daily_backward` OHLCV join + pandas 建帧。优化优先级：持久缓存 > 日分区合并（4,075 → 月/年文件）> 读连接线程 8~12 > 重新评估 OHLCV join 是否必要（涉及复权口径，需先验价）。

### D. 多模型并联（内存够时）

- 现在 `train_multi_models` 是**串行**。非 DL 6 个模型互相独立，可开 2~3 个并发（每模型 `TRAIN_NTHREADS≈cpu/workers`），28 分钟可压到 10~15 分钟；DL 7 个在单张 16GB GPU 上不建议盲目并发（DataLoader `num_workers=0` 是 CPU 瓶颈，2 个并发也难线性提速）。
- 13 模型实测：数据 ~3min（冷读）、非 DL ~28min（RF 965s / MLP 503s / LGB 131s）、DL ~152min（30 epoch 快筛），总 ~3h。持久缓存 + 非 DL 并行后可到 ~2h35m；DL 段不变。
- 更快的落地方式：拆成两个任务（非 DL 并行、DL 单独串行）共享持久缓存；不要在单进程里无脑开满线程。

### E. 快速补推（推理到最新）

- **模型管理「一键补全至最新」**与**模型治理「批量补推」**统一走 `/models/{id}/inference/backfill` 或 `/models/inference/backfill-jobs` → `create_job → run_serial → run_one_model`。
- 2026-11-28 起：模型之间仍串行，但**单模型内部 4 个交易日并发 + 全部缺口日只合并一次 `pred.parquet`**（`BACKFILL_DAY_WORKERS` 默认 4）；补推期间强制 `ENABLE_TDX_PUSH=false`，不触发通达信选股/滚动买卖。
- **单模型「批量推理」不是这条**：`/models/inference/batch` 走 `BatchInferenceOrchestrator`，前端默认并发 5、逐日独立子进程，**不写 pred.parquet**（只产出每日信号/run 记录）；用它补历史后 coverage 仍显示缺口是正常的。
- **代码改了必须重启 API 容器**：`model_backfill` 在 API 启动时 import，网页触发的补推用 API 进程里的旧模块；重启 `quantmind`/celery 后才切到新路径。

## Target Contract（训练目标契约，2026-11-24 起）

训练目标不再是散落的平铺字段，而是注册表里版本不可变的 `LabelTargetSpec`。提交训练时：

- 首选：`target_ref: {"id": "cn_ret_5d_open_rank", "version": 1}`；
- 允许覆盖：`label_overrides`（forward_return: `horizon_days` / `label_transform` / `label_top_pct` / `label_drop_extreme_pct`；classification: 仅 `horizon_days`；quantile: 无）；
- 重训复现：再加 `expected_target_hash`（原模型 metadata 的 `target_hash`）；覆盖后 hash 不一致 → 422 `target_hash_drift`；deprecated 目标只在此凭证下放行；
- 旧请求不带 `target_ref` 仍走 legacy 平铺字段，config 键集合与训练行为不变。

只读接口：
```bash
GET /api/v1/models/training/capabilities
GET /api/v1/models/training/targets?market=CN&status=active
GET /api/v1/models/training/targets/{id}/{version}
```
管理端：`POST /api/v1/admin/models/training/targets` + `/{id}/{version}/activate|deprecate`。

13 模型目标能力（提交/编排/容器三层硬闸）：
- classification：仅 lightgbm / xgboost / catboost / linear / random_forest / mlp；7 个 DL（gru/lstm/alstm/transformer/tcn/tabnet/nativetft）→ 422；
- quantile：仅 CN + 单 LightGBM + return；
- 排序损失：仅 lightgbm/xgboost/catboost（单模型）；
- sample_weight：lightgbm/xgboost/catboost/linear/random_forest；mlp/DL → 422；
- `execution_price=open` 必须有直读 QuantDB `factor_source`；`size_excess_*` 必须含 `total_cap`；
- classification 不允许 transform/drop_extreme；quantile 必须 raw。

容器侧标签诊断（训练前失败）：NaN>50% `target_label_incomplete`、分类正类率<1%/>99% `target_label_degenerate`、每日截面<50 `target_label_cross_section_too_small`、top-tail 实际比例偏离>50% `target_label_tail_unstable`；test 尾部不完整仅 warning。

模型 metadata 新增：`target_id/target_version/target_hash/target_base_hash/target_overrides/target_spec/label_contract_version/label_stats/contract_flags`。推理、成绩单、横评按 `target_hash` 分组；不同 hash 不可直接比较。legacy 模型 metadata 为 `target_id=legacy` + 合成 hash。

常见错误码：`target_not_found`、`target_deprecated`、`target_market_mismatch`、`target_override_not_allowed`、`target_capability_unsupported`、`target_objective_conflict`、`target_hash_drift`、`target_spec_hash_mismatch`、`target_spec_field_mismatch`、`target_label_*`。完整说明见 `docs/训练目标契约_使用说明_20261124.md`。

### Target Contract 下改周期 / 改口径

- 源 payload 带 `target_ref`：不要直接改 `target_horizon_days`（会被 spec 覆盖）。应加 `label_overrides={"horizon_days": 3}`；若是全新训练而非复现，删除 `expected_target_hash`。
- 源 payload 无 `target_ref`（legacy）：直接改平铺字段即可，行为与旧版一致。
- 复现原模型：保留 `target_ref` + 原训练 `label_overrides` + `expected_target_hash`；三者一致才通过 `target_hash_drift` 校验。

## 认证

```bash
BASE=http://localhost:8000
TOKEN=$(curl -s -X POST $BASE/api/v1/auth/login -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<管理员口令>","tenant_id":"default"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")
AUTH="Authorization: Bearer $TOKEN"
CT="Content-Type: application/json"
```

> ⚠️ **token 的 sub 决定模型归属**：登录签发的 JWT `sub=用户表 user_id`（如 admin 登录 → `00000001`），训练提交后模型注册到 `qm_user_models.user_id = sub`。**必须用登录接口拿 token**，不要手工构造 `sub='admin'` 之类的 token —— 否则训练模型归属错位，主栏"模型管理"（按登录用户过滤）看不到，只有后台（扫磁盘）能看到。```

---

## ① 训练模型（提交 T+N 模型训练）

### 1.0 支持的模型类型（13 种，端到端已验证）

| 类别 | 模型 | 说明 |
|---|---|---|
| 树模型 | lightgbm / xgboost / catboost / random_forest | 快，CPU/GPU 都行；RF 注意 max_depth 限制 |
| 线性 | linear | 简单基线，常过不了质量门禁 |
| 深度学习 | mlp / gru / lstm / alstm / transformer / tabnet / tcn / nativetft | GPU 训练（epoch1 CPU DataLoader 预热 3.5min 正常，之后 GPU 80s/epoch）；推理已走 CUDA |

提交时 `model_type` 传以上字符串，DL 模型用 `dl_params.n_epochs/batch_size`（冒烟建议 n_epochs=10、batch_size=8000，GRU~18min/LSTM~17.5min 跑完）。树模型用 `num_boost_round/early_stopping_rounds`。

### 1.1 从现有模型复刻（推荐：保特征，只改周期）

训练参数模型在 `admin_training_jobs.request_payload`。要训练 T+3 版本对比已有 T+5，**复制 T+5 的 payload 并只改周期**，保证特征/时间/数据完全一致，只变周期。**Target Contract（2026-11-24 起）**：若源 payload 带 `target_ref`，不能直接改 `target_horizon_days`（会被冻结 spec 覆盖）；应设 `label_overrides={"horizon_days": 3}`，并在“新训练”场景删除 `expected_target_hash`。源 payload 无 `target_ref`（legacy）时才可直接改平铺字段。

已有可复用脚本：`scripts/submit_t3_training.py`（从 T+5 复制改 T+3）

```bash
docker cp scripts/submit_t3_training.py quantmind:/tmp/
docker exec quantmind python3 /tmp/submit_t3_training.py
# 关键：从 admin_training_jobs 读 T+5 payload → target_horizon_days=3 → POST /models/run-training
```

核心逻辑（复制 payload）：
```python
async def get_t5_features():
    async with get_session() as s:
        r = await s.execute(text(
            "SELECT request_payload FROM admin_training_jobs WHERE id='train_XXXX'"))
        return json.loads(r.fetchone()[0])

t3 = get_t5_features()
t3["target_horizon_days"] = 3
t3["job_name"] = "L2_catboost_2023_2025T3"
t3["display_name"] = "L2 CatBoost T+3 (2023-2025训练)_CN"
# POST /models/run-training
```

### 1.2 训练提交后轮询状态

```bash
# runId 从提交响应拿 (train_YYYYMMDDHHMMSS_xxx)
curl -s -H "$AUTH" "$BASE/api/v1/models/training-runs/<runId>" \
  | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; print(d['status'], d['progress'])"
# 训练容器名: qm-train-<runId>
docker ps | grep qm-train
# 训练日志
docker exec quantmind python3 -c "
import asyncio
from backend.shared.database_manager_v2 import get_session
from sqlalchemy import text
async def m():
  async with get_session() as s:
    r = await s.execute(text(\"SELECT logs FROM admin_training_jobs WHERE id='<runId>'\"))
    print(str(r.scalar_one())[-500:])
asyncio.run(m())"
```

### 1.3 训练完成 → model 注册

训练完成 status=completed，模型注册为 `mdl_cn_train_<runId>`：
- 目录：`/app/models/users/default/admin/mdl_cn_train_*`（metadata.json + model.* + config.yaml + result.json + pred.pkl）
- 数据库：`qm_user_models` 表，`user_id` = 提交时 JWT 的 sub，`status` = ready / candidate（见 1.0 质量门禁）
- 查模型 ID：
```bash
# 最近注册的模型
curl -s -H "$AUTH" "$BASE/api/v1/research/models" | python3 -c "
import json,sys
for m in json.load(sys.stdin)['data']:
    print(m['modelId'], '|', m.get('name'))"
```

### 1.4 T+N 周期选择建议（来自实际回测）

| 周期 | 特点 |
|---|---|
| T+1 | 最灵敏，但噪音大、换手高、滑点损耗大 |
| **T+3** | **平衡**：比T+5敏锐，噪音可控（推荐首选尝试） |
| T+5 | 稳定，低频 |
| 判断 | 需要真实回测对比，不能凭感觉 |

### 1.5 严格化训练管线（2026-11-20，WP1–WP7）

训练提交与判优新增了四道闸，**判模型先看严格报告，不要只看 IC**。

#### 提交侧硬闸
- 请求特征在目录里有映射、但物理列不存在 → **422**（列出特征名，提示用 `库:列`，如 `tdx_ind:tdx_amihud`）。确实要跑残缺特征集才传 `allow_missing_features=true`（编排器只告警）。
- 训练结果页显示漏斗：`请求 N → 目录解析 M → 入模 K`；有缺失时红色告警条，并带训练血缘（git commit / config hash / 目录版本 / seed）。
- 排序损失与分位推理/Optuna 互斥：`objective=rank_xendcg|lambdarank` 时不要同时开 `prediction_mode=quantile` 或 `optuna.enabled`（提交期 422）。

#### 新增可调开关（payload 直传）
| 开关 | 作用 |
|---|---|
| `lgb_params.objective=rank_xendcg`（连续）或 `lambdarank`（10 档） | 排序损失；trainer 自动按 `trade_date` 分组，早停指标切 ndcg |
| `lgb_params.seed/bagging_seed/feature_fraction_seed` | 显式随机种子；训练页也提供 seed 输入（LGB/XGB/CAT 自动映射） |
| `sample_weight: {tradeable_mask: true, st: true, time_decay_half_life_days: 730}` | 不可交易/ST 样本权重置 0（标签仍按全市场截面算）；时间衰减只作用于 train；缺 mask/st 缓存时训练端 fail-closed |
| `label_drop_extreme_pct: 0.05` | train 段剔除两端 5% 极端标签 |

#### 训练完成后的两个后置产物（自动排队，失败不影响训练）
1. **严格评估** `strict_report`：信号日收盘出分 → T+1 开盘买入（可切 close 对照）→ T+1+N 收盘卖出；ST/停牌/涨停买不进按 `trade_mask` 剔除；成本 15/25/40bp 三档；统计 HAC t、block bootstrap 95% CI、PSR；组合层给净 Top-N（20/50/100/200/500）、MDD、月度、换手、容量参与率。
2. **数据审计** `audit_report`：未来列命名、标签契约（T+1/horizon/lag）、`trade_mask` 覆盖、ST 缓存覆盖、历史股票池幸存者代理、数据指纹、季度财务 PIT 探针摘要。

查询（用户态，需登录）：
```bash
curl -s -H "$AUTH" "$BASE/api/v1/models/training-runs/<runId>/strict-report"
curl -s -H "$AUTH" "$BASE/api/v1/models/training-runs/<runId>/audit-report"
# 未生成返回 404；训练结果页会自动展示（严格评估卡 / 数据审计卡）
# 管理员重算（后台异步）：
#   POST $BASE/api/v1/admin/models/training-runs/<runId>/strict-report
#   POST $BASE/api/v1/admin/models/training-runs/<runId>/audit-report
```

命令行回填/复算（容器内）：
```bash
docker exec -w /app quantmind python backend/scripts/strict_eval_run.py \
  --run-id <runId> --model-id <modelId> --store --write-standard
docker exec -w /app quantmind python backend/scripts/training_audit_run.py \
  --run-id <runId> --model-id <modelId> --store
```

#### 判优口径（替代「只看 IC」）
- **主判据**：测试段净 **Top20 @ 25bp > 0**，再看 MDD、超额（HS300/ZZ1000）、HAC t≥2、PSR≥0.8、月度不靠 1–2 个月。
- 2026-11-19 实例（开盘买入 / 25bp）：`100特征 L1V5均衡` 测试段 Top20 +11.6%（MDD −15.2%，HAC t 0.67）；`100特征 2010起` +9.7%（MDD −26.7%，HAC t 0.42）。**同收益、回撤差一倍**——这就是严格报告的价值。
- 数据审计里 `survivorship_universe` 常见 warning：pred 缺历史池 6.8% 股票，提示可能剔除了退市/长停股；不阻断但要在报告里写明。

#### A/B 对比与多种子
```bash
# 两个 run 同口径对比（配置 diff + 单轴判定 + 严格评估摘要）
curl -s -H "$AUTH" "$BASE/api/v1/models/training-runs/compare?ids=<runA>,<runB>"
# 训练结果页「对比其他 run」弹窗同款；只差一个轴才算严格 A/B

# 多种子顺序派发（单飞锁下串行，跑完自动汇总离散度）
docker exec -w /app quantmind python backend/scripts/multi_seed_dispatch.py \
  --base-run <runId> --seeds 42,7,2024 --submit
```

#### 晋升闸（模型 rollouts）
`POST /api/v1/models/rollouts/{id}/promote` 默认要求：
- 严格评估存在且测试段净 Top20(25bp) > 0；
- 数据审计 ready 且无 fail 级检查。
缺失/为负默认 400 阻断；确需放行要显式 `allow_strict_fail=true` / `allow_audit_fail=true`，warning 写进晋升审计。

> 运维前提：后置评估与审计跑在**应用容器**（读 `/data`），训练完成后自动排队；训练镜像只负责训练。`sample_weight` 的 trainer 改动需要重建训练镜像后生效。

---

## ② 批量推理历史（全年）

训练完成后，要对**历史全年**推理出每日分数（回测需要）。

### 2.1 批量推理 API（mode=range，全年逐日）

```bash
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/models/inference/batch" \
  -d '{
    "model_id": "<T+3_model_id>",
    "mode": "range",
    "start_date": "2026-01-06",
    "end_date": "2026-08-19",
    "top_k": 500,
    "reuse_existing": true
  }'
# 返回 {batch_id, status:"pending", trade_dates, window_meta}
```

> ⚠️ `/inference/batch` **只写每日信号 / run 记录，不写 `pred.parquet`**；补 pred 覆盖/图表请走「模型管理 → 一键补全至最新」或 `/models/inference/backfill-jobs`（2026-11-28 起：模型级串行，单模型内部 4 日并发 + 全部缺口日只合并一次 pred；`BACKFILL_DAY_WORKERS` 默认 4，补推强制 `ENABLE_TDX_PUSH=false`）。

### 2.2 轮询批量推理进度
```bash
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/batch/<batch_id>" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('status'), d.get('progress_done'), '/', d.get('progress_total'))"
```

### 2.3 验证推理结果（分数已写入 engine_signal_scores）

```bash
docker exec quantmind python3 -c "
import asyncio
from backend.shared.database_manager_v2 import get_session
from sqlalchemy import text
async def m():
  async with get_session() as s:
    r = await s.execute(text(\"SELECT MIN(trade_date),MAX(trade_date),COUNT(DISTINCT trade_date) FROM engine_signal_scores e WHERE e.run_id IN (SELECT run_id FROM qm_model_inference_runs WHERE model_id='<model_id>')\"))
    print(r.fetchone())
asyncio.run(m())"
# 预期 (2026-01-06, 2026-08-19, ~151)
```

> 注意：批量推理 range 模式耗时较长（每交易日全市场），耐心等待或后台跑。

---

## ③ 组合回测（自定义策略脚本）

### 3.1 脚本家族（`scripts/`）

| 脚本 | 用途 | 规则 |
|---|---|---|
| `backtest_l2_top20.py` | 基本版 | Top20每日滚动+每只100股+涨跌停禁买卖 |
| `backtest_l2_year.py` | 严格版 | 全年+滑点0.2%+T+1+涨跌停+ST剔除+资金分配 |
| **`backtest_l2_optimized.py`** | **优化版（推荐）** | 分数阈值+大盘MA20过滤+止损5%+ST剔除+滑点+T+1 |

> 与训练后置严格评估的分工：训练侧 strict_report 是「固定 H 日批次、同口径可比」的裁决口径；③ 的回测脚本是「策略参数（阈值/MA/止损）扫描」的研究口径。两者结论不一致时，以 strict_report 的可比口径做模型选择，再在脚本里找执行规则。

### 3.2 优化版策略规则（实测最有效）

```python
BUY_THRESHOLD = 0.015   # 买入门槛：分数 ≥ 0.015（避免垃圾区）
SELL_THRESHOLD = 0.005  # 卖出门槛：分数 < 0.005 卖出
STOP_LOSS = 0.05        # 止损：亏损5%卖出
MA_WINDOW = 20          # 大盘MA20过滤：大盘<MA不新买入
SLIPPAGE = 0.002        # 滑点0.2%
```

**三大风控（把 -40% 扭转到 +32%）**：
1. **分数阈值**：只买 ≥0.015 高分（胜率 82-92%），<0.005 卖出
2. **大盘 MA 过滤**：上证 <MA20 时空仓（躲开系统性下跌，回撤 51%→11%）
3. **止损 5%**：单票亏 5% 止损（控制单票风险）

> 注意：以上阈值来自 L2 家族的特定模型。换模型/换周期必须重跑 A/B，不要照搬。

### 3.3 运行回测（拷贝进容器，数据在 /data）

```bash
docker cp scripts/backtest_l2_optimized.py quantmind:/tmp/
# 临时改模型/时间：
#   MODEL_ID 改为目标模型的 id；START_DATE/END_DATE 设回测区间
# 或用 python -c 注入：
docker exec quantmind python3 -c "
import sys; sys.path.insert(0,'/tmp')
import backtest_l2_optimized as b
b.MODEL_ID = '<target_model_id>'
b.signals = b.load_signals()       # 自动读该模型全部信号
# ...然后 run_backtest + report
"
```

### 3.4 解读回测指标

| 指标 | 说明 |
|---|---|
| 累计收益 / 年化 | 年化短周期外推参考 |
| **最大回撤** | 流动性/风险核心，优化版目标 <15% |
| 夏普 / 索提诺 | 风险调整后收益 |
| 止损笔数 + 贡献 | 止损控风险的成本 |
| 大盘MA空仓天数 | 躲过多少系统性下跌 |
| 资金利用率 | 目标 >80%（资金没闲置） |
| 卖出胜率 | 单笔胜率（低是因为止损主动卖在-5%） |

---

## ④ 专业报告（MD → PDF）

### 4.1 报告生成器

```bash
# report_l2_optimized.py 复用回测数据生成研报级 MD
docker cp scripts/report_l2_optimized.py quantmind:/tmp/
docker cp scripts/backtest_l2_optimized.py quantmind:/tmp/
docker exec quantmind python3 -c "
import sys; sys.path.insert(0,'/tmp')
from report_l2_optimized import main; main()
"   # 生成 docs/L2_CatBoost_T5_优化回测报告.md
```

### 4.2 MD → PDF（研报级排版）

```bash
# md_to_pdf_report.py：深蓝封面+金色双线+斑马纹表格+红涨绿跌
docker cp "docs/xxx_report.md" quantmind:/tmp/rep.md
docker exec quantmind python3 /app/backend/scripts/md_to_pdf_report.py /tmp/rep.md "/tmp/xxx_report.pdf"
docker cp quantmind:/tmp/xxx_report.pdf "docs/xxx_report.pdf"
```

### 4.3 报告结构（9 部分）

1. **核心业绩指标**：收益/年化/回撤/夏普/索提诺/止损/空仓天数/资金利用
2. **阶段对比**：优化版 vs 基础版 vs 原严格版（体现改进）
3. **净值曲线**：逐日净值+日收益
4. **月度收益**：各月涨跌
5. **周度收益**：各周涨跌
6-7. **Top20 盈利/亏损股**：个股贡献
8. **交易明细**：买卖/价格/盈亏/原因
9. **策略规则说明**

> 2026-11-20 起，报告里的模型段应附上 strict_report 的净 Top20（三档成本）与数据审计结论；只有 IC 没有净收益的报告不再作为选型依据。

---

## 5. 完整工作流示例（对比 T+3 vs T+5）

```bash
# 1) 训练 T+3
docker exec quantmind python3 /tmp/submit_t3_training.py   # → runId
# 2) 等完成（轮询 training-runs）
# 3) 等严格评估就绪（GET .../strict-report；或 POST 管理员重算）
# 4) 推理全年
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/models/inference/batch" \
  -d '{"model_id":"<T+3_id>","mode":"range","start_date":"2026-01-06","end_date":"2026-08-19"}'
# 5) 回测 T+3 和 T+5（同一优化规则，各跑一次 backtest_l2_optimized）
# 6) 对比：优先用 /training-runs/compare 的严格评估与单轴 diff
# 7) 出报告：report_l2_optimized → md_to_pdf
```

---

## 6. 常见问题排查

| 问题 | 处置 |
|---|---|
| 训练进度卡住 | `docker ps` 看 qm-train 容器，`docker logs` 看 train.py 输出 |
| 提交 422「目录陈旧/特征缺失」 | 该特征在目录里有映射但物理列没了；用 `库:列` 限定（如 `tdx_ind:tdx_amihud`）或先刷新/重发目录；确需放行才 `allow_missing_features=true` |
| 实际入模特征远少于请求 | 看结果页漏斗与「缺失特征」红条；再看 `factor_selection.stage_counts`（IC/相关/PFS/DH 各剩多少） |
| 严格评估 404 / 未生成 | 后置任务需要 API 进程已加载新代码；管理员 `POST .../strict-report` 重算，或容器内跑 `strict_eval_run.py --store` |
| 排序损失提交 422 | rank_xendcg/lambdarank 与 quantile/optuna 互斥，先关掉后者 |
| 样本权重训练直接报错 | `trade_mask.parquet` / `st_pit_cache` 缺失（fail-closed）；确认 `/data/quantdb` 对应路径存在 |
| 推理慢/超时 | 先确认走的是补推还是 `/inference/batch`：补推（写 pred）2026-11-28 起单模型内 4 日并发 + 一次合并；batch（不写 pred）默认并发 5。后台跑，`reuse_existing=true` 断点续跑 |
| 网页点补推仍然很慢 | API 容器没重启，进程里还是旧 `model_backfill`；重启 `quantmind`/celery 后看日志 `[backfill-fast] ... workers=4` |
| 补推期间出现通达信推送/桥超时 | 补推脚本应设 `ENABLE_TDX_PUSH=false`；批量补历史不得触发实盘选股/滚动买卖 |
| 批量训练结果没图表 / eval 退化成 label_rank | 旧 DL 子模型 pred 只有 5 列（缺 `label_return/OHLC/控制列`）；2026-11-28 起已修，需重训才有完整列。直跑 `train.py` 还没有 `qm_training_runs` 记录，网页训练历史看不到——改走 `/models/run-training` |
| 训练读数据慢（冷读 2 分钟+） | 训练缓存未命中；直跑 config 把 `cache.dir` 设为 `/data/training_cache`，平台 Docker 用 `/training_cache`；同特征/窗口重复跑约 15s |
| 回测无买入 | 检查 MODEL_ID 是否对、分数是否 ≥0.015（分数太低自然空仓） |
| 报告收益异常 | 确认 klines 覆盖区间、涨跌停/停牌处理、cron 时间 |
| asyncio 冲突 | 混用 load_signals(asyncio) 和 psycopg2 时，加载函数统一用 psycopg2 同步 |
| 年化离谱 | 短周期外推失真，看累计收益和回撤，别信年化 |
| 晋升被 400 阻断 | 严格评估净 Top20≤0 或数据审计 fail；先看结果页两张卡，确需放行用 `allow_strict_fail/allow_audit_fail` |
| 训练完主栏模型管理看不到 | 后台"模型管理"= 扫磁盘目录（148+ 个、按 updated_at 倒序、打开自动扫描）；主栏"模型管理"= 按登录用户查 qm_user_models。看不到先查归属：`SELECT user_id FROM qm_user_models WHERE model_id='mdl_cn_train_...'`，若 user_id ≠ 登录账号的 user_id，需 UPDATE 转移 |
| 模型 status=candidate | 质量门禁未过（IC 不达标），正常流程非失败；CatBoost/Linear/NativeTFT 常落此档，推理/回测仍可用 |
| DL 训练慢/卡 epoch1 | epoch1 CPU DataLoader 预热 3.5min 属正常，之后 GPU 满载 80s/epoch；若一直不动看 qm-train 容器日志 |
| 训练/推理为何 CPU 参与 | epoch1 窗口构造在 CPU；旧版推理全量 CPU 11min，新版 CUDA 推理已优化，勿回退 |

## 7. 相关技能

- **[[backtest-center]]** — Qlib 单策略回测/专家模式/参数优化（不同的回测层面）
- **[[batch-inference-analysis]]** — 分析推理结果选股（本技能的重心是验证策略收益）
- **[[quantmind-operations]]** — 模型训练/模型管理的运营操作
- **[[ai-ide-strategy-writing]]** — AI 写策略并执行

## 8. 模型管理（训练完去哪看模型）

| 入口 | 数据源 | 说明 |
|---|---|---|
| 后台管理 → 推理引擎 → 模型管理（admin/models/scan） | 扫磁盘 /app/models 目录 | 148+ 个目录，按 updated_at 倒序（最新在前），打开页面自动扫描；5 分钟 Redis 缓存，`?refresh=true` 强刷；列表显示模型类型 + 训练任务名(job_name) |
| 主栏 → 模型管理（GET /models） | qm_user_models 表按当前登录用户 | 只显示本用户模型；今天新训练的模型需要归属 user_id=登录用户才可见（见认证 ⚠️） |

- 归档：qm_user_models `UPDATE ... SET status='archived'`（每用户唯一 is_default 约束：`uq_qm_user_models_default_per_user`，改默认前先摘旧）
- 训练任务：admin_training_jobs（request_payload 可复刻、logs、result）
- 严格评估：`qm_training_strict_reports`（run_id → report JSONB）
- 数据审计：`qm_training_audit_reports`（run_id → report JSONB, blocked）

## 9. 相关脚本

- `scripts/submit_t3_training.py` — 复刻 T+5→T+3 训练提交
- `scripts/backtest_l2_optimized.py` — 优化版组合回测（分数阈值+MA+止损）
- `scripts/backtest_l2_year.py` — 全年严格版
- `scripts/backtest_l2_top20.py` — 基础版
- `scripts/report_l2_optimized.py` — 研报级 MD 生成
- `backend/scripts/strict_eval_run.py` — 严格评估（净 Top-N/三档成本/HAC/PSR）回填与落库
- `backend/scripts/training_audit_run.py` — 数据审计（PIT/泄漏/可成交）
- `backend/scripts/multi_seed_dispatch.py` — 多种子顺序派发 + 离散度
- `backend/scripts/md_to_pdf_report.py` — MD→PDF 研报排版
