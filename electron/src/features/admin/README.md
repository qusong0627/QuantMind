# Admin Feature Notes

## 训练特征字典数据覆盖扩展（2026-04-04）

- `AdminModelFeatureCatalog` 类型新增可选字段 `data_coverage`，用于承载后端返回的特征快照覆盖信息：
  - `min_date/max_date`：本地特征快照可用日期范围；
  - `suggested_periods(train/val/test)`：前端训练页可直接消费的建议时间窗；
  - 其余辅助字段：`snapshot_dir/file_count/scanned_files/failed_files/total_rows`。

## 策略模板管理默认参数

- 2026-03-29：管理员新建模板默认代码中的 TopK 调仓参数更新为 `topk=50, n_drop=10`，与全局 20% 调仓比例口径保持一致。

## 仪表盘鉴权防抖（401 熔断）

- 管理面板 `GET /api/v1/admin/dashboard/metrics` 返回 401 时，前端会触发“401 熔断锁”：
  - 本次会话内停止对该接口的重复请求，避免控制台/后端日志刷屏；
  - 页面提示“登录已过期，请重新登录后再访问管理面板”；
  - 需重新登录或刷新页面后再恢复请求。

## 模型管理：生成明日信号前置检测

- “生成明日信号”按钮新增前置条件检测弹窗：
  - 检测数据交易日（`data_trade_date`）
  - 预测生效交易日（`prediction_trade_date`，明日）
  - 生产模型目录是否存在
  - 模型文件是否存在（默认 `model.bin`，兼容 `model.txt/pkl/etc`）
  - `metadata.json` 是否存在
  - `db/qlib_data` 目录是否存在
  - 检测目标交易日（按交易日历推导）
  - 推理脚本存在（主模型/兜底模型至少一套）
  - 生产模型期望特征维度（动态解析，非固定 48）
  - `market_data_daily` 可查询性
  - `market_data_daily` 维度探测自动兼容 `features(JSONB)` 与 `feature_*` 列
  - 目标交易日数据入库情况、覆盖数阈值（自适应）
  - 满足“模型期望维度”的覆盖数阈值（自适应）
- 门禁改为“硬阻断 + 提醒项”：
  - 仅硬阻断项失败时禁用“继续并生成明日信号”；
  - 其余失败项仅提示风险，不阻断执行。
- 写入口径：
  - `engine_signal_scores.trade_date` 统一为 `prediction_trade_date`（预测生效日）；
  - 预测数据默认保留最近 30 天。
- 弹窗中缺失项支持一键处理：
  - 模型目录/文件/元数据缺失：`立即重新扫描`
  - Qlib 数据目录缺失或数据就绪检查失败（交易日/入库/覆盖阈值/维度覆盖阈值）：`去回测中心补数据`
  - `market_data_daily_query`（查询异常）不提供跳转，需先排查数据库可用性
- 2026-04-07 补充：
  - 管理端前置检查与手动推理已接入交易日历中心，不再只依赖 `exchange_calendars` 本地口径；
  - 返回新增 `requested_inference_date` 与 `calendar_adjusted`，用于标识“候选日期是否被自动回退到最近交易日”。

## 数据管理：Qlib/数据库状态看板

- 管理后台新增“数据管理”页签，对齐后端接口 `GET /api/v1/admin/models/data-status`。
- 页面展示：
  - Qlib 日历范围、标的分布（SH/SZ/BJ）、特征目录统计；
  - 最新交易日覆盖统计（`at_target/older/invalid`）；
  - 异常标的 TopN 明细：`older`（滞后样本）与 `invalid`（结构异常样本）；
  - `market_data_daily` 最新交易日、最新更新时间、今日行数、`feature_*` 列数；
  - “最新交易日是否与系统交易日一致”状态标签。
- 页面操作：
  - `从Baostock补基础数据`：调用 `POST /api/v1/admin/models/sync-market-data-daily`，手动触发 Baostock 基础数据回填 `market_data_daily`。
- 支持手动刷新，便于日常巡检“增量补数是否完成”。

## 数据管理：官方增量同步（2026-05-05）

- 数据管理页新增“官方增量更新配置”卡片：
  - 支持填写 `API Base URL`、`Access Key`、`Secret Key`、可选 `Version`；
  - 支持“生成配置脚本”，便于运维直接在服务器执行；
  - 支持“一键更新”，调用后端 `POST /api/v1/admin/models/sync-official-data-update` 触发增量包拉取与应用。
- 后端执行脚本：`backend/scripts/sync_official_data_update.py`，用于：
  - 调用 `/api/v1/data-updates/latest|{version}` 获取签名下载信息；
  - 下载并解压 `bundle`；
  - 同步 `db/feature_snapshots`、`db/qlib_data`、`docs`；
  - 若包含 `db_deltas/stock_daily_latest*.parquet`，执行数据库 upsert。

## 预测管理：预测批次查询

- 管理后台新增“预测管理”页签：
  - 查询接口：`GET /api/v1/admin/models/predictions`
  - 明细接口：`GET /api/v1/admin/models/predictions/{run_id}`
- 支持按预测交易日、`run_id`、`tenant_id`、`user_id` 过滤。
- 支持查看批次统计（条数、分数范围、写入时间）与 symbol 级分数明细。

## 模型管理：训练目标元数据展示

- `AdminModelManagement.tsx` 现在会从 `metadata.json`、`workflow_config.yaml` 或 `qlib_config` 中解析训练目标口径：
  - `target_horizon_days`
  - `target_mode`
  - `label_formula`
  - `training_window`
- 模型列表、详情弹窗和 workflow 摘要都会展示 `T+N`，用于和训练页保持一致口径。
- 老模型如果没有上述字段，会自动回退为 `—`，不会影响目录扫描和详情查看。
