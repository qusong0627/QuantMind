# 数据边界与不做的事（source-boundary，本地版）

本技能只对**已存在的因子面板**做漂移体检，不预测、不喊单、不修改任何数据。

## 允许的数据来源

- **本地 QuantDB（quantdb 模式，主路径）**：只读本地 parquet——
  - 因子面板：`<root>/{quantdb|quanthk|quantus}/6_ml_datasets/<dataset>/dt=*/data.parquet`（CN 含 `features_daily/l1_factors/l2_factors`，HK/US 为 `l1_factors`）；
  - 行情参照：`<root>/{quantdb|quanthk|quantus}/1_kline_data/daily_forward`（只读分区日期列表，不读行情数值）。
- **内置合成面板（`--demo`）**：脚本内确定性生成，用于自检与断言，不含任何真实证券或市场观测。
- 数据根目录自动探测 `/data`、`/quantmind/data`、`/home/zbox/projects/quantmind/data`，可用 `QM_DATA_ROOT` 覆盖。
- 报告写入 `/data/reports/factor-drift-monitor/`（容器内属主 root；报告是产物，不进仓库）。

## 不允许

- 伪造数据、用随机样本冒充正式结论；
- 修改、回填、删除任何 parquet 或数据库内容（本技能全程只读）；
- 把真实数据导出或报告提交进仓库；
- 把「分布漂移」直接当结论——必须区分数据故障与真实市场状态变化（报告 caveats 内置此声明）。

## 不做的事

- 不构造/筛选因子、不回测、不评估 IC（那是 factor-ic-decay 等技能的职责）；
- 不自动修复数据（发现故障后交给数据管线负责人）；
- 不对缺失/常量列做插补或「修正后统计」——缺失率按原始列如实计算，inf 只从统计量中过滤并单独计数（`profile.inf_cells`）；
- 白名单（`--allow-removed/--allow-added`）只用于**已确认的上游改版**，不得用于掩盖疑似故障。

## 与源技能的边界差异

源技能读取 PandaData SDK / 用户 CSV；本地版砍掉 SDK 依赖，只保留：本地 parquet 逐分区直读、单一 CSV 语义（parquet 即面板）、确定性合成自检。源技能的 IC 保留率（IC retention）检查未移植——本地 IC 口径由 factor-ic-decay 技能负责，避免两处口径漂移。
