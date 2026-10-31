# 模型训练与数据操作参考

## 数据流转链

```
外部数据源（QuantDB SDK / Yahoo Finance / Binance / akshare）
      ↓  各市场 *_daily_sync.py
本地 parquet（data/quantdb/、data/quanthk/、data/quantus/、data/quantbc/、data/quantfutures/）
      ↓  quantdb_daily_sync 的 fill_pg_from_parquet（DuckDB join + execute_values）
PostgreSQL stock_daily_latest（A股快照表）
      ↓  qlib_data_builder.py（QlibDataBuilder.for_market）
Qlib 二进制缓存（.qlib_cache/{cn,hk,us,bc,futures}_data）
      ↓  generate_feature_snapshots.py（A股按年）/ update_feature_parquet.py（多市场）
特征快照（db/feature_snapshots/model_features_{year|market}.parquet + .metadata.json）
      ↓  模型训练 / 推理 / 回测
```

## 数据平台结构（五市场）

| 市场 | 数据目录 | Qlib 缓存 | 特征快照 | 主数据源 |
|------|---------|-----------|----------|----------|
| **A股** | `data/quantdb/` | `.qlib_cache/cn_data` | `model_features_{year}.parquet`（按年） | QuantDB SDK |
| **港股** | `data/quanthk/` | `.qlib_cache/hk_data` | `model_features_hk.parquet` | Yahoo + akshare + CCASS |
| **美股** | `data/quantus/` | `.qlib_cache/us_data` | `model_features_us.parquet` | Yahoo Finance |
| **区块链** | `data/quantbc/` | `.qlib_cache/bc_data` | `model_features_crypto.parquet` | Binance |
| **期货** | `data/quantfutures/` | `.qlib_cache/futures_data` | `model_features_futures.parquet` | akshare |

- **QuantDB 数据目录**：`data/quantdb/`（6 大类：`1_kline_data` / `2_base_sector` / `3_financial` / `4_bond_etf` / `5_technical_derived` / `6_ml_datasets`）
- **字段路由**：`config/data_sources/field_routing.yaml`（quantdb_local 为主源，旧适配器兜底）
- **A股同步 4 阶段**：`sync_parquet()`（V2 分区 `dt=YYYYMMDD/data.parquet` 增量）→ `fill_pg_from_parquet()` → `update_qlib_cache()` → 年度特征快照
- **特征快照**：`generate_feature_snapshots.py --year YYYY` 从 QuantDB parquet 直读 `daily_backward + features_daily + l1_factors + l1_l2_factors`，JOIN 后写按年 parquet + metadata.json（含 Alpha158/GTJA 扩充、去泄漏列）
- **覆盖日期**：2016-01 起；A股特征快照逐年，非A股单体文件

## 同步调度

- **定时同步**：`/api/v1/admin/data-platform/sync-schedule/{market}` 配置（enabled/time/days/datasets/with_qlib），存 Redis `quantmind:sync_schedule:{market}`
- **每日任务**：`engine.tasks.daily_data_sync`（22:30）、`update_qlib_cache`（22:40）、`feature_snapshot`（22:50）
- **多市场调度**：`engine.tasks.dispatch_market_sync` 每分钟检查各市场定时配置

## 训练管线

```
run-training → submit_training_job → LocalDockerOrchestrator
  → 校验 features 在 parquet 存在性
  → 启动训练容器 (Docker)
  → 产出模型到 /models
  → deploy_to_production 决定是否入生产
```

### 模型类型（13 种）

| 类别 | 模型 |
|---|---|
| 树/线性 | lightgbm / xgboost / catboost / linear / random_forest |
| 深度学习 | gru / lstm / alstm / transformer / tabnet / tcn / nativetft |
| 其他 | mlp（sklearn） |

> `hybrid_gru_tree` 已剔除（QLIB map 无实现）；以 `backend/shared/training/request.py::ALLOWED_MODEL_TYPES` 为准。

**集成法**：none / stacking / blending / voting
**参数**：features(≤300)、target_horizon_days(1-30)、target_mode(return/classification)、lgb/xgb/catboost/dl_params、wfa（walk-forward）
> 已下线/死配置：`horizons`（多周期，2026-09 清理）、`optuna`、`n_folds`（不参与序列化）。

### 关键 payload 字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `model_type` | lightgbm | 上述 13 种 |
| `model_types` | — | 多模型列表（ensemble 用） |
| `ensemble` | none | none/stacking/blending/voting |
| `features` | [] | 特征 key 列表（从 feature-catalog 取，≤300） |
| `target_horizon_days` | 1 | 预测 horizon |
| `target_mode` | return | return / classification |
| `optuna` | — | Optuna 超参搜索配置 |
| `context` | — | {initial_capital, benchmark, commission_rate, slippage, deal_price, market, industry_as_feature} |

## 模型注册表

- **表**：`qm_user_models`（status: candidate/syncing/ready/active/archived/failed, metadata_json, metrics_json, is_default）
- **用户模型**：`/api/v1/models`（用户态 CRUD）
- **市场分段**：model_id=`mdl_{market_lower}_{run}_{digest}`，非CN存 `models/users/{tenant}/{user}/{market_lower}/`，market∈CN/a_share、HK、US、CRYPTO、FUTURES
- **系统模型回退**：系统内置 `model_qlib/alpha158` 已废弃，不再有隐式回退
- **融合模型**：`/api/v1/models/ensemble/create` 已下线（路由不存在）；多周期 + 手工融合于 2026-09 清理（`backend/scripts/cleanup_multi_horizon_ensemble.py`）。仅保留历史融合模型的推理兼容（`ensemble_config.json` / `inference_ensemble_src.py`）

## 推理链路

```
precheck-inference → inference/run（单日）→ inference/batch（range/lookback 批量）
  → 信号表 trade_date = T+1
  → inference/runs（历史） / inference/stock/{symbol}/history（单股）
  → 批量聚合 aggregate_batch（per_symbol/groups/movers/daily/meta，含 IC/趋势/共识带）
  → 推理质量回填（qm_model_inference_quality，生产 IC 监控）
```

## 运维注意事项

1. **数据新鲜度**：`/admin/data-platform/freshness` 查看各表最新日期；交易日收盘后需同步
2. **特征快照**：`generate_feature_snapshots.py --year YYYY` 或 `/admin/models/data-status` 查看年度详情（A股 metadata.json）
3. **同步并发**：daily-sync 是 Celery 异步任务，提交后轮询 `/daily-sync/status/{task_id}`
4. **服务健康**：`/health` 检查 api/engine/trade/stream 四服务
5. **Qlib 更新**：`/sync-datasets?with_qlib=true` 或 `engine.tasks.update_qlib_cache` 增量重建
