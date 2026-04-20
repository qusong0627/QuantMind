# Inference Module

本模块负责生产模型加载与推理，默认目录为 `models/production`。

## 修复记录（2026-03-27，自动推理 Celery 任务）

- 修复 `InferenceScriptRunner._query_dimension_readiness` 中 `SessionLocal` 未定义导致的自动任务异常：
  `NameError: name 'SessionLocal' is not defined`。
- 处理方式：在该函数内与写库链路一致，显式基于 `DATABASE_URL` 构造同步 SQLAlchemy 会话（`+asyncpg -> +psycopg2`），仅用于就绪度查询。
- 影响：`engine.tasks.auto_inference_if_needed` 不再因该异常中断，可继续执行日常自动推理流程。

## 修复记录（2026-04-14，自动推理模型解析同步化）

- 自动推理 Celery 路径与 `run_daily_inference_script()` 已统一改为同步模型解析：
  `ModelRegistryService.resolve_effective_model_sync()`。
- 处理方式：在 Celery worker 场景下不再通过 `asyncio.run()` 调用 async 模型解析，避免
  `Task ... got Future ... attached to a different loop`。
- 影响：自动推理在解析策略绑定/用户默认模型时更稳定，推理成功后才会继续写入
  `qm:signal:latest:{tenant_id}:{user_id}` 并触发交易侧托管任务。

## 每日信号生成统一流程（2026-03）

“明日信号生成”支持三种触发方式：

- 管理端手动触发：`POST /api/v1/admin/models/run-inference`
- 策略激活后异步触发：`POST /api/v1/strategies/{strategy_id}/activate` 成功后，按当前 `tenant_id + user_id(8位)` 触发一次推理并发布到对应 Signal Stream
- 自动兜底触发：Celery Beat 每日 08:55 触发 `engine.tasks.auto_inference_if_needed`

执行器统一为 `InferenceRouterService + InferenceScriptRunner`，并支持用户模型动态解析：

1. 解析优先级：`显式 model_id > 策略绑定 > 用户默认 > model_qlib > alpha158`
2. 用户模型目录：`models/users/{tenant}/{user}/{model_id}`
3. 系统主模型目录：`models/production/model_qlib`，主数据源：`db/qlib_data`
4. 系统兜底模型目录：`models/production/alpha158`，兜底数据源：`db/qlib_data`（运行时会解析为 `/app/db/qlib_data`）
5. 用户模型失败时会回落系统链路，保持 `model_qlib -> alpha158` 兜底
4. 结果统一写入 `engine_feature_runs/engine_signal_scores` 并写 Redis 完成标记 `qm:inference:completed:{prediction_trade_date}`
5. 同步写入最新可消费版本键 `qm:signal:latest:{tenant_id}:{user_id}`，交易侧 runner 会据此丢弃旧 `run_id` 的过期信号，确保只消费最新推理结果
5. 脚本写库链路在 `DATABASE_URL` 为 `postgresql+asyncpg://...` 时，会自动转换为同步驱动 `postgresql+psycopg2://...` 执行写入，避免 `greenlet_spawn` 异常

统一日期口径：

- `data_trade_date`：推理输入数据交易日（用于维度门禁与脚本 `--date`）
- `prediction_trade_date`：信号生效交易日，固定取 `data_trade_date` 的下一交易日（明日）
- `engine_signal_scores.trade_date` 与 `engine_feature_runs.trade_date` 均写入 `prediction_trade_date`
- `qm:signal:latest:{tenant_id}:{user_id}` 保存最近一次可消费的 `run_id`，runner 只接受该版本对应的 stream 消息
- 激活触发、管理员触发、Celery 触发三条链路统一使用上述口径

数据保留策略：

- `inference_script` 写库链路默认只保留最近 30 天预测数据；
- 可通过 `INFERENCE_PREDICTION_RETENTION_DAYS` 调整保留窗口。

维度与覆盖门禁说明：

- 期望特征维度动态解析优先级：
  1. `metadata.json` 的 `feature_count/feature_dim/input_dim/feature_columns/input_spec.tensor_shape`
  2. `feature_schema.json` 的 `features/feature_columns/columns`
  3. 推理脚本注释中的“XX 特征”
  4. `INFERENCE_DEFAULT_FEATURE_DIM`（默认 48）
- 覆盖阈值采用自适应规则（不再固定“>=3000”）：
  - `INFERENCE_MIN_READY_SYMBOLS`（默认 3000）
  - `INFERENCE_MIN_READY_RATIO`（默认 0.9）
  - `INFERENCE_MIN_READY_FLOOR`（默认 100）

统一返回元信息（四条入口一致）：

- `fallback_used`
- `fallback_reason`
- `active_model_id`
- `effective_model_id`
- `model_source`
- `active_data_source`

其中 `/api/v1/inference/predict` 与 `/api/v1/pipeline/runs/{run_id}/result` 均可直接消费这些字段；
`pipeline` 同时会将上述字段落盘到 `result_json` 顶层，便于审计与追踪。

## 已支持模型

- LightGBM（`framework=lightgbm`）
- PyTorch / TFT（`framework=pytorch`）

## TFT Native 接入规范

`tft_native` 模型目录建议包含：

- `metadata.json`（基础信息）
- `inference_metadata.json`（推理专用信息，包含 `framework/model_file/input_spec`）
- `model_best.pth`（state_dict）

加载顺序：

1. 读取 `metadata.json`
2. 若存在 `inference_metadata.json`，补齐缺失键
3. 按 `framework` 选择加载器

### 序列输入格式

当 `input_spec.tensor_shape` 为 3 维（如 `[null, 30, 54]`）时，推理服务按序列模型处理。

支持两种输入：

- 直接序列：`{"sequences": [[[...]]], "symbols": ["SZ000001"]}`
- 行数据：包含 `instrument/symbol` + `date/datetime` + 特征列，系统按标的分组后自动组装最近 `lookback_window` 天序列

输出：

- `predictions`: 每个序列对应一个标量分数
- `symbols`: 与输入序列一一对应

## 运行依赖

若使用 `framework=pytorch`，运行环境必须安装 `torch`。
