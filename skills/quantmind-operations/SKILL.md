---
name: quantmind-operations
description: "QuantMind 平台运营操作技能 — 覆盖模型训练、模型管理、后台数据更新、字段信息查询、RSS 新闻对接与分析。在 QuantBot / Claude Code 中处理模型训练、数据同步、新闻分析等任务时使用。触发词：模型训练、模型管理、数据更新、字段信息、RSS、新闻分析、训练模型、查看数据、同步数据"
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

# QuantMind 运营操作技能

QuantMind 量化平台的完整运营操作指南。所有 API 都通过 API 网关（默认 `http://127.0.0.1:8000` 或 `http://192.0.2.68:3080`）访问，统一加 `/api/v1` 前缀。

## 认证

所有请求需要 Bearer Token：
```bash
# 获取 token（admin 账号）
TOKEN=$(curl -s -X POST $BASE/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<管理员口令>","tenant_id":"default"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))")

# 通用请求头
AUTH="Authorization: Bearer $TOKEN"
CT="Content-Type: application/json"
```

## 1. 模型训练（5 步流程）

模型训练分 **5 步**，与前端 ModelTrainingPage 一致：
```
特征选择 → 训练目标 → 参数配置 → 执行训练 → 结果入库
```

### 1.1 特征选择（筛选输入因子）
```bash
# 获取特征字典（类别/数量由 QuantDB l1_factors 动态生成，以接口返回为准）
curl -s -H "$AUTH" "$BASE/api/v1/models/feature-catalog"

# 带数据覆盖统计（含建议训练/验证/测试区间）
curl -s -H "$AUTH" "$BASE/api/v1/models/feature-catalog?include_coverage=true"

# 管理端特征字典（含扫描详情）
curl -s -H "$AUTH" "$BASE/api/v1/admin/models/feature-catalog"
```
选择特征 key 列表（如 `["mom_ret_5d", "vol_std_20"]`）或按类别（`feature_categories`）。
特征类别由后端特征目录**动态生成**，随 QuantDB `l1_factors` 数据版本变化（示例 version `20260831` 返回 10 类 110 特征：`momentum` / `fundamental` / `money_flow` / `style` / `technical` / `turnover` / `concept` / `volatility` / `chip` / `industry`）。**先读接口返回的 `categories[].id`，不要硬编码类别清单。**

### 1.2 训练目标（定义 T+N 标签口径）
- `target_horizon_days`：预测周期（T+1 / T+5 / T+20 等）
- `target_mode`：`regression`（回归）或 `classification`（分类）
- `label_formula`：标签计算公式（如 `close_future/close - 1`）
- `effective_trade_date`：生效交易日
- `training_window`：训练窗口（如 `rolling`）

### 1.3 参数配置（设置超参与训练上下文）
- **时间划分**：`train_start/end`、`valid_start/end`、`test_start/end`、`val_ratio`
- **模型超参**：`num_boost_round`、`early_stopping_rounds`、`lgb_params`/`xgb_params`/`catboost_params`/`dl_params`
- **训练上下文 `context`**：`initial_capital`、`benchmark`、`commission_rate`、`slippage`、`deal_price`、`market`、`industry_as_feature`

### 1.4 执行训练（编排请求与日志预览）
```bash
curl -s -X POST "$BASE/api/v1/models/run-training" -H "$AUTH" -H "$CT" -d '{
  "model_type": "lightgbm",
  "model_types": ["lightgbm", "xgboost", "catboost"],
  "ensemble": "stacking",
  "job_name": "我的模型",
  "display_name": "我的模型",
  "train_start": "2022-01-01",
  "train_end": "2024-12-31",
  "valid_start": "2023-06-01",
  "valid_end": "2024-06-30",
  "test_start": "2024-07-01",
  "test_end": "2024-12-31",
  "val_ratio": 0.15,
  "num_boost_round": 1000,
  "early_stopping_rounds": 100,
  "features": ["mom_ret_5d", "vol_std_20"],
  "feature_categories": ["momentum", "volatility"],
  "target_horizon_days": 1,
  "target_mode": "regression",
  "label_formula": "close_future/close - 1",
  "effective_trade_date": "2025-01-02",
  "training_window": "rolling",
  "context": {"initial_capital": 1000000, "benchmark": "000300.SH", "commission_rate": 0.0003, "slippage": 0.001, "deal_price": "close", "market": "CN", "industry_as_feature": false},
  "deploy_to_production": false
}'
```
**支持的 model_type（13 种，以 `backend/shared/training/request.py::ALLOWED_MODEL_TYPES` 为准）**：
- 树/线性：`lightgbm` / `xgboost` / `catboost` / `linear` / `random_forest`
- 深度学习：`gru` / `lstm` / `alstm` / `transformer` / `tabnet` / `tcn` / `nativetft`
- 其他：`mlp`（sklearn 实现）
- ⚠️ `hybrid_gru_tree` 已剔除（QLIB map 无实现），**勿再传**（会被 422 拒绝/落入不支持）。

**ensemble 取值**：`none` / `stacking` / `blending` / `voting`（多模型训练时生效）
**可选高级参数**：`wfa`（walk-forward，`rolling`/`expanding`）、`target_horizon_days`（单周期 T+N，1–30）、各模型专属超参 `lgb_params`/`xgb_params`/`catboost_params`/`dl_params`
> ⚠️ 已下线/死配置：`horizons`（多周期，2026-09 随多周期训练一并清理）、`optuna`、`n_folds`（只建类型不参与序列化，传了不生效）。
**返回**：`runId` + 有效/缺失特征统计

### 1.5 结果入库（查看元数据与产物）
```bash
# 轮询训练状态（pending→running→completed/failed）
curl -s -H "$AUTH" "$BASE/api/v1/models/training-runs/{run_id}"

# 训练完成后模型进入 /models，可设为默认
curl -s -X PATCH -H "$AUTH" -H "$CT" "$BASE/api/v1/models/default" -d '{"model_id":"xxx"}'

# 查看模型列表确认入库
curl -s -H "$AUTH" "$BASE/api/v1/models"
curl -s -H "$AUTH" "$BASE/api/v1/models?include_archived=true"

# 系统内置模型
curl -s -H "$AUTH" "$BASE/api/v1/models/system-models"

# ⚠️ 手工融合模型已下线：/api/v1/models/ensemble/create 路由不存在
# （多周期 + 手工融合已于 2026-09 清理，见 backend/scripts/cleanup_multi_horizon_ensemble.py；
#   仅在多模型训练时用 model_types + ensemble 合成，或保留历史融合模型做推理兼容）
```

## 2. 模型管理（管理端）

### 2.1 扫描本地模型目录
```bash
curl -s -H "$AUTH" "$BASE/api/v1/admin/models/scan"
```

### 2.2 数据状态
```bash
# Qlib + 特征快照数据状态
curl -s -H "$AUTH" "$BASE/api/v1/admin/models/data-status"
```

### 2.3 推理前置检查（生成明日信号）
```bash
curl -s -H "$AUTH" "$BASE/api/v1/admin/models/precheck-inference"
```

### 2.4 滚动回测
```bash
curl -s -X POST "$BASE/api/v1/admin/models/backtest" -H "$AUTH" -H "$CT" -d '{
  "model_id": "xxx",
  "start": "2024-01-01",
  "end": "2024-12-31"
}'
# 可用回测日期
curl -s -H "$AUTH" "$BASE/api/v1/admin/models/backtest/trading-dates"
# 回测历史
curl -s -H "$AUTH" "$BASE/api/v1/admin/models/backtest/history/{model_id}"
```

### 2.5 推理回测（选股策略事件驱动）
```bash
curl -s -X POST "$BASE/api/v1/admin/models/inference-backtest" -H "$AUTH" -H "$CT" -d '{
  "model_id": "xxx"
}'
```

## 3. 后台数据更新（五市场）

### 3.1 统一日同步（推荐）
```bash
# 提交同步任务，返回 task_id（market: A/CN=QuantDB, US=QuantUS, HK=QuantHK, BC=区块链, FUTURES=期货）
curl -s -X POST "$BASE/api/v1/admin/data-platform/daily-sync" -H "$AUTH" -H "$CT" -d '{
  "market": "A",
  "symbols": [],
  "incremental": true,
  "calibrate": true
}'
# 查询同步状态
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/daily-sync/status/{task_id}"
```

**各市场同步数据源**：
| 市场 | market 值 | 数据源 | 说明 |
|---|---|---|---|
| A股 | A / CN | QuantDB SDK | 4阶段：parquet→PG→Qlib→特征快照 |
| 美股 | US | Yahoo Finance | `quantus_daily_sync.py` |
| 港股 | HK | Yahoo + akshare + CCASS | `quanthk_daily_sync.py` |
| 区块链 | BC | Binance | `quantbc_daily_sync.py`（支持 --minute） |
| 期货 | FUTURES | akshare | `quantfutures_daily_sync.py` |

### 3.2 定时同步调度（每市场独立配置）
```bash
# 查看全部市场定时配置
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/sync-schedule"
# 查看单市场配置
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/sync-schedule/{market}"
# 保存单市场配置（enabled/time/days/datasets/with_qlib）
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/admin/data-platform/sync-schedule/{market}" \
  -d '{"enabled": true, "time": "22:30", "days": [1,2,3,4,5], "datasets": ["all"], "with_qlib": true}'
# 立即触发一次同步（测试）
curl -s -X POST -H "$AUTH" "$BASE/api/v1/admin/data-platform/sync-schedule/{market}/run"
```

### 3.3 同步状态 / 进度
```bash
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/sync-status"
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/sync-progress"
```

### 3.4 Qlib 同步（增量重建缓存）
```bash
# 同步数据集时带 with_qlib 触发 Qlib 缓存重建
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/admin/data-platform/quantdb/sync-datasets" \
  -d '{"datasets":["l1_factors","l2_factors"],"with_qlib":true}'
# 查看 Qlib + 特征快照数据状态（含年度快照详情）
curl -s -H "$AUTH" "$BASE/api/v1/admin/models/data-status"
```
**Qlib 路径**（`QlibDataBuilder.for_market`）：A股 `.qlib_cache/cn_data`，HK/US/BC/FUTURES 各目录下 `.qlib_cache/{hk,us,bc,futures}_data`。

### 3.5 特征快照（更新特征 parquet）
```bash
# 指定年份（A股按年生成 model_features_{year}.parquet）
curl -s -X POST -H "$AUTH" "$BASE/api/v1/admin/data-platform/update-feature-parquet?year=2026"
# 多市场特征更新
curl -s -X POST -H "$AUTH" "$BASE/api/v1/admin/data-platform/update-market-features"
# 特征快照年度详情（A股逐年 metadata.json）
curl -s -H "$AUTH" "$BASE/api/v1/admin/models/data-status"
```
**特征快照结构**：A股 `db/feature_snapshots/model_features_{year}.parquet`（含 `.metadata.json` 年度详情），非A股单体 `model_features_{market}.parquet`。

### 3.6 基本面同步 / 数据新鲜度
```bash
curl -s -X POST -H "$AUTH" "$BASE/api/v1/admin/data-platform/sync-fundamentals"
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/freshness"
```

### 3.7 在线状态 / 数据源健康
```bash
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/online-status"
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/sources"
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/sources/{name}/health"
```

### 3.8 万得 L2 原始数据导入（手动，不走 daily-sync）
逐笔委托/成交/十档盘口由 `wind_l2_import.py` 从万得逐日 7z 手动导入
（schema/单位/坑见 [[quantdb-fields]] 第三章，含深市成交量≈2×、tick_data 单位混源）：
```bash
python backend/scripts/wind_l2_import.py --archive /path/to/20260511.7z                  # 全市场
python backend/scripts/wind_l2_import.py --archive /path/to/20260511.7z --symbols 000001.SZ
```
落盘 `1_kline_data/l2_data/order_|trade_{code}_{date}.parquet` + `tick_data/{code}_{date}.parquet`；
**文件名即日期**（`20260511.7z` → 20260511），增量跳过已存在，`--force` 覆盖，可断点续跑。

## 4. 字段信息

### 4.1 字段覆盖矩阵（市场 × 字段 × 源）
```bash
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/health-matrix?market=A"
```

### 4.2 字段覆盖表
```bash
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/field-coverage"
```

### 4.3 质量告警
```bash
curl -s -H "$AUTH" "$BASE/api/v1/admin/data-platform/quality-alerts"
```

### 4.4 支持的字段类别（特征字典）
通过 `/api/v1/models/feature-catalog` 获取。类别与特征数**由 QuantDB `l1_factors` 动态生成**（示例 version `20260831` 返回 10 类 110 特征），随数据版本变化——**以接口返回为准**，不要硬编码类别清单。

## 6. 推理研究（推理中心 + 推理历史）

推理研究涵盖：单日推理、批量多日推理、批量单日推理、推理历史、股票历史分数。

### 6.1 推理前置检查
```bash
# 生成明日信号前置检查（确认数据就绪）
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/precheck"
```

### 6.2 单日推理（核心）
```bash
# 对指定模型在指定日期执行推理（可能耗时数分钟）
curl -s -X POST "$BASE/api/v1/models/inference/run" -H "$AUTH" -H "$CT" \
  -d '{"model_id":"xxx", "inference_date":"2026-08-07"}' \
  -w "\nHTTP %{http_code}\n"
```

### 6.3 批量推理（单日批量 / 多日批量）

批量推理支持**两种模式**，提交后立即返回 `batch_id`，逐日推理在后台执行：

**A. 批量单日推理（range 模式）**——区间内每个交易日逐日执行单日推理
```bash
curl -s -X POST "$BASE/api/v1/models/inference/batch" -H "$AUTH" -H "$CT" -d '{
  "model_id": "xxx",
  "mode": "range",
  "start_date": "2026-08-01",
  "end_date": "2026-08-07",
  "top_k": 20,
  "side": "both"
}'
```

**B. 批量多日推理（lookback 模式）**——锚定日回溯 N 个交易日
```bash
curl -s -X POST "$BASE/api/v1/models/inference/batch" -H "$AUTH" -H "$CT" -d '{
  "model_id": "xxx",
  "mode": "lookback",
  "anchor_date": "2026-08-07",
  "window_days": 30,        # 默认 = 模型 horizon，所有信号梯队仍持有中
  "top_k": 20,
  "side": "both",
  "reuse_existing": true
}'
```

**参数完整说明**：
| 参数 | 取值 | 说明 |
|---|---|---|
| `mode` | `range` / `lookback` | range=日期区间逐日；lookback=锚定日回溯窗口 |
| `start_date` / `end_date` | YYYY-MM-DD | range 模式必填，区间内逐日推理 |
| `anchor_date` | YYYY-MM-DD | lookback 模式必填 |
| `window_days` | 整数 | lookback 回溯天数（默认=模型 horizon） |
| `top_k` | 整数 | 每日排名前 N 名 |
| `side` | `long` / `short` / `both` | 多/空/双向 |
| `reuse_existing` | 布尔 | 复用已存在的推理结果 |
| `concurrency` | 整数 | 并发度 |

**返回**：HTTP 202 + `batch_id`。之后用 batch_id 轮询进度。

### 6.4 批量推理历史与进度
```bash
# 批量推理历史
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/batches?page=1&page_size=20"
# 单个批次进度（status: pending/running/completed/failed）
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/batch/{batch_id}"
# 删除批次记录
curl -s -X DELETE -H "$AUTH" "$BASE/api/v1/models/inference/batch/{batch_id}"
```

### 6.5 批量推理实战流程
1. **确认模型**：`/models/default` 或 `/models` 选模型
2. **提交**：range（指定区间）或 lookback（锚定+窗口）
3. **轮询**：`/inference/batch/{batch_id}` 查进度，completed 后取结果
4. **汇总**：批量结果含每日信号，可对比多日信号变化
5. **清理**：不需要的批次 DELETE

### 6.6 推理历史（单日推理记录）
```bash
# 推理历史（支持按 run_id/状态/日期过滤）
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/runs?model_id=xxx&page=1&page_size=20"
# 单次推理结果明细（排名/信号/行业等）
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/runs/{run_id}"
# 删除推理记录
curl -s -X DELETE -H "$AUTH" "$BASE/api/v1/models/inference/runs/{run_id}"
```

### 6.7 单只股票历史推理分数
```bash
# 某股票的历史推理分数趋势（用于交叉验证选股）
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/stock/600036.SH/history?days=180"
```

### 6.8 推理自动设置 / 最新批次
```bash
# 自动推理设置（每日定时）
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/settings/{model_id}"
curl -s -X PUT -H "$AUTH" -H "$CT" "$BASE/api/v1/models/inference/settings/{model_id}" -d '{"auto_enabled": true}'
# 当前生效推理批次
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/latest"
```

### 6.9 批量聚合分析（推理分析）
```bash
# 某批次的聚合分析（per_symbol/groups/movers/daily/meta，含 IC/趋势/共识带）
curl -s -H "$AUTH" "$BASE/api/v1/models/inference/batch/{batch_id}/aggregate"
```

### 6.10 融合模型 pred 生成（回测信号）
融合模型（`ensemble_config.json`）本身无 pred.pkl，AI-IDE 回测/信号生成时会**自动**调用 `generate_ensemble_pred`：读取子模型 pred.pkl → 按 (datetime, instrument) 对齐 → 截面排名百分位加权融合 → 落到融合模型目录 `pred.pkl`。单模型无 pred 时提示"请先推理"。

## 7. RSS 新闻对接与分析

### 5.1 新闻源列表
```bash
curl -s -H "$AUTH" "$BASE/api/v1/news/sources"
# 返回: {sources: [{source_id, source_name, subscribe_url, type, folder_id, folder_name}], folders, total}
```

### 5.2 拉取新闻文章（核心接口，支持丰富过滤）
```bash
curl -s -H "$AUTH" "$BASE/api/v1/news/articles" \
  -G \
  --data-urlencode "tickers=600519.SH,000858.SZ" \
  --data-urlencode "industries=白酒,消费" \
  --data-urlencode "sentiment=bullish" \
  --data-urlencode "event_tags=财报,业绩预增" \
  --data-urlencode "keyword=茅台" \
  --data-urlencode "sort=sentiment_bullish" \
  --data-urlencode "since=2026-08-01T00:00:00Z" \
  --data-urlencode "page=1"
```
**过滤参数**：
- `source_id` / `source_ids` — 新闻源过滤
- `folder_id` — 文件夹过滤
- `keyword` — 标题关键词
- `tickers` — 股票代码（逗号分隔）
- `industries` — 行业
- `sentiment` — `bullish` / `bearish` / `neutral`
- `event_tags` — 事件标签（财报/业绩预增/减持等）
- `countries` / `regions` — 国家/地区
- `key_terms` — 关键词（AI/半导体等）
- `date_entities` — 提及日期
- `starred` — 仅收藏
- `strong_only` — 仅强信号（|score|>=0.5）
- `sort` — `time_desc`（最新）/ `time_asc` / `sentiment_bullish`（利好强度）/ `sentiment_bearish`（利空强度）

### 5.3 单篇文章详情
```bash
curl -s -H "$AUTH" "$BASE/api/v1/news/articles/{article_id}"
```

### 5.4 新闻富化统计 / 触发富化
```bash
curl -s -H "$AUTH" "$BASE/api/v1/news/enrichment/stats"
curl -s -X POST -H "$AUTH" "$BASE/api/v1/news/enrichment/run"
curl -s -X POST -H "$AUTH" "$BASE/api/v1/news/enrichment/rebuild-all"
```

### 5.5 刷新新闻源
```bash
curl -s -X POST -H "$AUTH" "$BASE/api/v1/news/sources/{source_id}/refresh"
```

## 8. 实战分析流程（推荐顺序）

当用户要求分析某股票/行业时，按此流程：
1. **查新闻**：`/news/articles` 带 tickers + sentiment + since，看利好/利空
2. **查模型分数**：`/models/inference/stock/{symbol}/history` 看历史推理分数趋势
3. **查数据健康**：`/admin/data-platform/health-matrix?market=A` 确认数据完整
4. **查当前模型**：`/models/default` 确认生效模型
5. **需要更新数据**：`/admin/data-platform/daily-sync` 提交增量同步
6. **需要训练**：先 `feature-catalog` 拿字段，再 `run-training`

当用户要求**挖掘新因子**时，使用 [[rd-agent-factor-mining]] 技能（RD-Agent 自动演化管线）。
当用户要求**按条件选股 / 筛选股票池**时，使用 [[smart-strategy-stock-picking]] 技能（基于 QuantDB 字段字典的条件选股）。
当用户要求**查询 QuantDB 数据 / 配置 API Key / 查看数据集字段**时，使用 [[quantdb-sdk]] 技能。
当用户要求**深度分析市场 / 数据挖掘 / 导出分析数据 / 生成投研报告**时，使用 [[stock-market-analysis]] 技能。
当用户要求**运行回测 / 对比策略 / 参数优化 / 分析回测结果**时，使用 [[backtest-center]] 技能。
当用户要求**用 AI 写策略 / 生成 Qlib 策略代码**时，使用 [[ai-ide-strategy-writing]] 技能。
当用户要求**模拟交易 / 下单 / 查持仓**时，使用 [[simulation-trading]] 技能。
当用户要求**分析批量推理结果 / 解读信号 / 选股决策 / 负分参考**时，使用 [[batch-inference-analysis]] 技能。
当用户要求**生成投研报告 / 深度研报 / 多Agent分析**时，使用 [[trading-agents]] 技能。

## 9. 相关技能

- **[[rd-agent-factor-mining]]** — 自动调用 RD-Agent 挖掘因子（evolve/tasks/factors/backtest/export）
- **[[smart-strategy-stock-picking]]** — 基于 QuantDB 数据的条件选股（自然语言/条件/DSL 三种方式）
- **[[quantdb-sdk]]** — QuantDB 数据 SDK（API Key 配置、28 数据集目录、字段查询、远程查询、同步）
- **[[stock-market-analysis]]** — 市场深度分析 + 数据导出（全市场扫描/行业轮动/个股371字段/风险评分/CSV导出）
- **[[backtest-center]]** — 回测中心（快速回测/专家模式/策略对比/参数优化/高级分析/向量化极速回测）
- **[[ai-ide-strategy-writing]]** — AI-IDE 写策略并执行（Docker runner 运行/回测）
- **[[simulation-trading]]** — 模拟交易（下单买卖/持仓/成交/账户/模拟盘启动）
- **[[batch-inference-analysis]]** — 批量推理结果分析（市场状态/选股/负分参考/行业轮动）
- **[[trading-agents]]** — 投研分析（多 Agent 研究报告、7 分析师、风险评估）

## 10. 常见排查

| 现象 | 排查 |
|---|---|
| 特征字典加载失败 | `/models/feature-catalog` 返回是否 200，看服务健康 |
| 数据匹配不到 | `/admin/data-platform/health-matrix` 看字段覆盖，`/freshness` 看新鲜度 |
| 新闻空白 | `/news/sources` 确认源存在，`/news/enrichment/stats` 看富化状态 |
| 训练失败 | `/models/training-runs/{run_id}` 查状态，看 features 是否在 parquet 中存在 |
