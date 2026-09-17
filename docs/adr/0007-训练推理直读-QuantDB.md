# ADR-0007 训练/推理直读 QuantDB 因子源（快照 parquet 冻结）

- 状态：已采纳（2026-08-15 单源迁移起；2026-09-17 实时基线并入）
- 背景：`model_features_{year}.parquet` 派生链多代污染（格式混用/停更/坏 TTM）；
  云端三类因子源（features_daily / l1_factors / l2_factors）成为训练/推理事实源。
- 决定：读因子一律 `QuantDBFactorReader`（绑定 `data_source=quantdb_factors` 的模型）；
  遗留快照 parquet 只服务旧模型的不可变快照语义（实时基线按模型绑定分派，
  `load_baseline_for_model`，commit 3d5cdfae）；`QM_ENABLE_LEGACY_FEATURE_SNAPSHOT`
  仅 break-glass。
- 后果：parquet 冻结不再维护（勿信其"最新"）；新模型训练默认直读；因子源新鲜度成为
  全链 SLA（看门见体检 C13 计划）。
