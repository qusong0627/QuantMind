# 训练配置文件 schema（quantmind-model-training-config）

本文件是**前端 `parseTrainingConfig` / `buildTrainingConfigFile` 的事实源镜像**
（源码：`electron/src/pages/training/trainingUtils.tsx`，测试：
`electron/src/pages/training/__tests__/trainingConfigFile.test.ts`）。
生成配置时逐字段对照本表。

## 顶层字段

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `schema_version` | ✅ | `1` | 固定为字面量 `1`；不匹配直接拒绝导入 |
| `kind` | ✅ | string | 固定 `quantmind-model-training-config` |
| `exported_at` | 建议 | ISO8601 | 仅元数据，导入不校验 |
| `market` | ✅ | enum | `CN` / `HK` / `US` / `CRYPTO` / `FUTURES`；决定导入时是否自动切市场 |
| `factor_source` | 可选 | string | QuantDB 直读市场用，如 `l1_factors` / `l1_l2_factors`；导入后会切换页面的因子源 |
| `factor_catalog_version` | 可选 | string | **仅供导入时提示版本是否变化**；实际训练用页面当前已发布版本，不是回放 |
| `factor_filter` | 可选 | object | 因子筛选节点，见下 |
| `configuration` | ✅ | object | 主体配置，见下 |

### `factor_filter`

| 字段 | 类型 | 默认 | 合法区间（导入时钳制） |
|---|---|---|---|
| `enabled` | bool | `true` | — |
| `n_top` | int | `80` | `[10, 300]` |
| `ic_threshold` | float | `0.01` | `[0, 1]` |
| `icir_threshold` | float | `0.15` | `[0, 5]` |
| `correlation_threshold` | float | `0.9` | `[0.1, 1]` |

> 阈值语义：`|IC| ≥ ic_threshold` 且 `|ICIR| ≥ icir_threshold` 才保留，再按 |IC| 取 `n_top` 个，
> 并对相关性 > `correlation_threshold` 的做剪枝。`n_top` 大于候选特征数时筛选等于空转（全保留）。

## `configuration` 字段

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `displayName` | 建议 | string | 模型显示名；空则训练时自动生成 |
| `displayNameMode` | 可选 | `auto`\|`manual` | 默认 `auto`；手动命名填 `manual` |
| `selectedFeatures` | ✅ | string[] | 因子 key 列表，**非空**、不能含重复/标签类字段 |
| `timePeriods` | ✅ | object | `{train,val,test}`，每项 `["YYYY-MM-DD","YYYY-MM-DD"]` |
| `target` | ✅ | object | `{mode, horizonDays}` |
| `params` | ✅ | object | 模型与超参，键见下 |
| `context` | ✅ | object | 训练上下文 |
| `wfa` | 可选 | object | 滚动窗口稳定性诊断，默认关闭 |

### `timePeriods` 规则

- 三段都必须存在且日期可解析（推荐 `YYYY-MM-DD`；ISO 字符串也可）。
- 必须满足 `train_end < val_start` 且 `val_end < test_start`，否则导入即报错。
- **间隔 `gap ≥ horizonDays + 1`**：小于时后端会自动把 val/test 起点后移（不报错，但真实区间变了）。

### `target`

| 字段 | 类型 | 取值 |
|---|---|---|
| `mode` | enum | `return`（回归，默认）/ `classification`（二分类） |
| `horizonDays` | int | `>= 1`，即 T+N 的 N |

### `params`（模型与超参）

`model_type` 必填，13 选 1：

```
lightgbm  xgboost  catboost  linear  random_forest
gru  lstm  alstm  transformer  tabnet  tcn  nativetft  mlp
```

**只有下列键会被前端保留**（不在列表内的键导入时静默丢弃）：

| 分组 | 允许键 |
|---|---|
| 通用 | `model_type` `model_types` `prediction_mode` `ensemble_method` `num_boost_round` `early_stopping_rounds` `objective` `metric` |
| LightGBM | `learning_rate` `num_leaves` `max_depth` `min_data_in_leaf` `path_smooth` `bagging_freq` `lambda_l1` `lambda_l2` `feature_fraction` `bagging_fraction` |
| XGBoost | `xgb_max_depth` `xgb_subsample` `xgb_colsample_bytree` `xgb_reg_alpha` `xgb_reg_lambda` `xgb_min_child_weight` |
| CatBoost | `cb_depth` `cb_l2_leaf_reg` `cb_random_strength` `cb_bagging_temperature` `cb_od_wait` |
| 线性 | `linear_alpha` |
| 随机森林 | `rf_n_estimators` `rf_max_depth` `rf_max_features` |
| 深度学习 | `dl_hidden_size` `dl_num_layers` `dl_dropout` `dl_n_epochs` `dl_batch_size` `dl_lr` `dl_step_len` `tcn_kernel_size` `tft_num_heads` |
| Stacking | `n_folds` `meta_alpha` |

> ⚠️ `lgb_learning_rate` / `lgb_max_depth` / `xgb_learning_rate` / `cb_learning_rate`
> 不在允许表里，写了也会被丢弃。LGB 用共享的 `learning_rate`/`max_depth`；
> XGB/CatBoost 的 lr 也只认共享 `learning_rate`。

枚举：

- `prediction_mode`: `point`（默认）| `quantile`
- `ensemble_method`: `none`（默认）| `stacking`（需 `model_types` ≥ 2）
- `objective`: `regression` | `binary`
- `metric`: `l2` | `rmse` | `mae` | `auc` | `binary_logloss`
- `rf_max_features`: 字符串，如 `sqrt` / `log2` / 数字字符串

DL 默认值（未写时按模型族自动补）：

| 模型 | hidden | layers | dropout | lr | batch | epochs | step_len |
|---|---|---|---|---|---|---|---|
| gru/lstm/alstm | 64 | 2 | 0.2 | 1e-3 | 4000 | 200 | 20 |
| transformer | 64 | 2 | 0.2 | 1e-4 | 4000 | 200 | 20 |
| tabnet | 64 | 5 | 0.2 | 5e-3 | 4000 | 200 | 20 |
| tcn | 128 | 2 | 0.2 | 1e-4 | 4000 | 200 | 20 |
| nativetft | 64 | 2 | 0.2 | 5e-4 | 4000 | 200 | 20 |
| mlp | 64 | 2 | 0.2 | 1e-4 | 4000 | 200 | 20 |

### `context`

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `initialCapital` | number | `1000000` | 初始资金 |
| `benchmark` | string | 按市场 | CN=`SH000300` HK=`HSI` US=`SPX` CRYPTO=`BTC` FUTURES=`CL.FUT` |
| `commissionRate` | number | `0.00025` | 佣金 |
| `slippage` | number | `0.0005` | 滑点 |
| `dealPrice` | enum | `open` | `open` / `close` |
| `market` | enum | — | 与顶层 `market` 保持一致 |
| `industry_as_feature` | bool | `false` | 是否把行业作为特征（CatBoost 受益） |

### `wfa`

| 字段 | 类型 | 默认 |
|---|---|---|
| `enabled` | bool | `false` |
| `strategy` | `rolling`\|`expanding` | `rolling` |
| `nWindows` | int | `4` |
| `trainYears` | number | `3` |
| `valMonths` | number | `12` |
| `stepMonths` | number | `12` |

## 导入时前端实际做了什么（决定“哪些字段有用”）

1. 解析并校验 `kind` / `schema_version` / `market` / 日期顺序 / 模型类型白名单 / 数值钳制。
2. 预览提示三类风险：当前目录没有的 `selectedFeatures`、市场是否变化、目录版本是否变化。
3. 确认后整体 `HYDRATE` 覆盖表单：特征、时间、目标、params、context、wfa、股票池、显示名。
4. `factor_filter` 是独立状态，单独同步；老配置无此节点则沿用页面当前值。
5. 若 `market` 变了 → 自动切市场；若 `factor_source` 变了 → 重载因子目录；
   不在当前目录里的特征会被过滤掉（预览已提示）。
6. `factor_catalog_version` **不写回表单**，只用于版本变化告警。

## 后端会二次规整（提交时）

- 只认白名单字段；`auto_feature_filter` 关闭时不再注入 `factor_selection`。
- 若 `train_end`/`val_end` 与后段间隔 < `horizonDays + 1`，自动向后平移后段起点并记入
  `system_notices`（不会 422）。
- QuantDB 直读市场（`factor_source` 非空）必须携带当前已发布 `factor_catalog_version`，
  否则 422；特征必须在已发布目录内，否则 422。
