# quantmind-api

业务聚合服务（认证、用户、社区、通知、管理、选股等）。

## 入口
- 应用入口：`/Users/qusong/git/quantmind/backend/services/api/main.py`
- 默认端口：`8000`
- 数据库配置来源：仅项目根目录 `.env`（服务目录 `.env` 不再维护 `DATABASE_URL`）。
- 启动初始化：`quantmind-api` 启动时仅加载统一配置与数据库连接，不再在服务启动阶段自动建表或播种管理员数据；Schema 迁移与 bootstrap 需在发布流程中显式执行。

## 模块边界（P1）
- `api` 仅负责统一入口与横切能力：鉴权、限流、审计、路由聚合、服务代理。
- `api` 自有业务域：`/api/v1/auth/*`、`/api/v1/users/*`、`/api/v1/profiles/*`、`/api/v1/admin/*`、`/api/v1/inquiry`、`/api/v1/files|execute|ai|config/*`（AI-IDE 代理）。
- `api` 代理到 `engine`：`/api/v1/strategies*`、`/api/v1/strategy*`、`/api/v1/qlib/*`、`/api/v1/analysis*`、`/api/v1/inference/*`、`/api/v1/pipeline/*`、`/api/v1/stocks/*`、`/api/v1/selection/*`、`/api/v1/strategy-backtest-loop/*` 以及 `templates/validate/providers/performance/market/legacy` 相关路径。
- `api` 代理到 `engine`：已补充 `/api/v1/analysis*` 转发，支持高级分析页面直连网关调用。
- `api` 代理到 `trade`：`/api/v1/orders/*`、`/api/v1/trades/*`、`/api/v1/portfolios/*`、`/api/v1/simulation/*`、`/api/v1/real-trading/*`。
- `api` 代理到 `trade`：已补充 `/api/v1/internal/strategy/*`（用于 Agent 下载与内部策略网关转发）。
- `OpenClaw` 旧后端模块已下线：`quantmind-api` 不再挂载历史 `/api/openclaw/*` 本地路由。
- 当前测试接入方式：`quantmind-api` 新增 QuantBot 适配层，对前端统一暴露 `/api/v1/openclaw/*`，由网关转发到 `COPAW_BASE_URL`。当前代码默认值与生产建议值均为容器网络地址 `http://quantbot:8088`，避免回退到历史外网端口。
- 当前测试范围仅包含聊天、会话、历史消息与健康检查；不接入 cron jobs、模型、技能、工具、工作空间等管理能力。
- 当前测试阶段不配置上游访问密钥，若后续 QuantBot 增加鉴权，统一在网关适配层补充，不由前端直连处理。
- 官网联系表单 `POST /api/v1/inquiry` 已直连 `quantmind-api`，提交内容会落到 `data/inquiries.json`，无需额外部署 website-server。
- 管理员仪表盘 `GET /api/v1/admin/dashboard/metrics` 现会聚合 `quantmind-api`、`quantmind-trade`、`quantmind-engine`、`quantmind-stream` 的真实 `/health` 状态生成 `health_score`，并结合 API 启动时间计算 `uptime_days`；如探测全部失败则降级为 `0` 分和 `degraded` 状态。
- 禁止范围：`api` 不直接承载计算引擎、交易撮合与行情推送实现。
- 管理端模型管理已继续拆分：`routers/admin/model_management.py` 仅保留模型 CRUD 和推理链路入口，通用扫描/校验/运维逻辑迁入 `routers/admin/model_management_utils.py` 与 `routers/admin/model_management_ops.py`。

## 本次重构更新（2026-02-18）
- 已完成 `api/community` 本地化：社区模块迁入
  `backend/services/api/community_app/`，入口已直连 `community_app`（适配层已物理移除）。
- 已完成 `api/stock_query` 本地化：选股模块迁入
  `backend/services/api/stock_query_app/`，入口已直连 `stock_query_app`（适配层已物理移除）。
- 已完成 `api/user` 本地化：用户模块迁入
  `backend/services/api/user_app/`，入口已直连 `user_app`（适配层已物理移除）。
- `services/api` 内已不再直接导入历史旧服务模块。
- 新增统一 Schema 注册归口：`api.community` 与 `api.user`
  由 `backend/shared/schema_registry.py` 统一登记，便于迁移与巡检。
- 新增 AI-IDE 代理路由：`/api/v1/files/*`、`/api/v1/execute/*`、`/api/v1/ai/*`、`/api/v1/config/*`，
  由 `backend/services/api/routers/ai_ide_proxy.py` 转发到 `AI_IDE_SERVICE_URL`。
  - 例外：`/api/v1/files/upload` 与 `/api/v1/files/delete` 由 API 网关本地路由直接处理（用于个人中心头像上传/删除），不再透传 AI-IDE。
  - **Docker 部署注意**：由于 AI-IDE 运行在宿主机（随 Electron 启动），容器内需配置为 `http://host.docker.internal:8010` 才能绕过网络隔离。
- 网关 CORS 已收敛到统一白名单解析：
  - 开发/测试环境：默认允许 `localhost/127.0.0.1` 的 `3000/3001/5173` 前端源。
  - 生产/预发环境：必须显式配置 `CORS_ALLOWED_ORIGINS`（或兼容变量 `CORS_ORIGINS`），禁止使用 `*`。
  - 未配置时生产环境默认拒绝浏览器跨域请求（fail-closed）。
- AI-IDE 代理在上游不可达时返回 `502` JSON（`AI-IDE upstream unavailable`），避免未处理异常导致的网关 `500`。
- 认证登录流程已增加降级保护：当 Redis/Sentinel 不可用或审计存储异常时，登录限流/失败计数/审计写入不会阻断登录主流程，避免 `/api/v1/auth/login` 被非核心依赖放大为 500。
- 修复登录计数字段兼容性：`_finalize_login()` 现兼容 `users.login_count` 为 `NULL` 的历史/手工导入账号，避免此类账号在密码校验通过后因 `login_count += 1` 触发 `TypeError`，表现为 `/api/v1/auth/login` 返回 `500`。
- 修复认证令牌校验中的会话过期时间比较异常：兼容 `UserSession.expires_at` 的 naive/aware 时间，避免访问 `/api/v1/profiles/me/profile` 时触发 `TypeError` 导致 500。
- 修复会话过期时间写入时区错误：`user_sessions.expires_at` 改为写入 `timezone.utc` 感知时间，避免 `GET /api/v1/admin/dashboard/metrics` 等受保护接口在登录后立即出现 `401 Token已过期`。
- P3 可观测性基线：统一注入并透传 `X-Request-ID` 响应头，便于跨服务链路追踪。
- P3 错误契约：统一错误结构 `error.code/error.message/error.request_id`，并兼容保留 `detail` 字段。
- P3 日志基线：统一访问日志字段 `service/request_id/tenant_id/user_id/method/path/status/duration_ms`。
- P3 指标基线：新增 `/metrics`（Prometheus），统一暴露 `quantmind_service_health_status{service="quantmind-api"}` 与 `quantmind_service_degraded{service="quantmind-api"}`。
- P3 健康语义：`/health` 在关键依赖初始化失败时返回 `status=degraded`，与 `quantmind_service_degraded` 指标保持一致。
- P3 代理错误映射：`engine/trade/ai_ide` 上游连接失败统一 `503`，超时统一 `504`，其他上游传输异常统一 `502`，并输出统一错误契约结构。
- P3 OpenAPI 收敛：网关代理透传路由（含 `/api/v1/qlib/*`、`/api/v1/strategies/*`、`/api/v1/simulation/*`、`/api/v1/files/*` 等）统一 `include_in_schema=False`，避免重复 operationId 噪音，文档仅保留网关真实业务接口。
- P3 兼容清理：用户/通知/管理相关响应模型已切到 `ConfigDict(from_attributes=True)`，`UserRegister` 校验器已切到 `@field_validator`，避免 Pydantic V2 弃用告警在 smoke/CI 中放大。

## 网关安全更新（2026-02-20）
- 修复 `engine_proxy` 导入问题：补齐 `typing.Optional` 依赖，避免模块加载时报 `NameError`。
- 修复 `trade_proxy` 导入问题：补齐 `typing.Optional` 依赖，避免 `api-server` 启动阶段异常退出。
- `strategy` 代理收敛鉴权：`/api/v1/strategy*` 现在强制要求 Bearer Token（匿名请求返回 `401`）。
- 身份头防伪造：网关不再透传外部 `X-User-Id`、`X-Tenant-Id`、`X-Internal-Call`，仅在鉴权成功后注入可信身份头。
- 网关到引擎转发补充内部鉴别头：当 `strategy` 请求已鉴权时，自动注入 `X-Internal-Call`（值来自 `INTERNAL_CALL_SECRET`），与引擎侧内部鉴权中间件保持一致。
- 长耗时代理超时分级：`/api/v1/strategy/generate*` 默认使用更长读超时（`ENGINE_PROXY_LLM_TIMEOUT_SECONDS`，默认 `600s`），其余引擎接口仍使用 `ENGINE_PROXY_TIMEOUT_SECONDS`（默认 `120s`），避免 LLM 生成在网关层被提前截断。
- 引擎代理上游默认地址调整为 `http://127.0.0.1:8001`（替代 `localhost`），并在 `localhost` 连接失败时自动回退重试 IPv4 地址，降低本地解析异常导致的 `503 engine upstream unavailable` 概率。

## 管理端模型推理更新（2026-02-27）
- 管理后台手动推理接口 `POST /api/v1/admin/models/run-inference` 新增 `model_file` 参数（默认 `model.bin`）。
- 接口会将 `model_file` 限制在 `models/production/model_qlib/` 目录内并校验存在性，避免路径越界与误调用。
- 前端管理端默认传入 `model_file=model.bin`（参数保留兼容；实际执行入口仍为 `inference.py`）。
- 新增推理前置检查接口 `GET /api/v1/admin/models/precheck-inference`：除模型目录/文件存在性外，额外校验目标交易日数据就绪状态（最新交易日、当日入库、模型维度完整性与覆盖阈值）。
- 日期口径统一为“双日期”：
  - `data_trade_date`：用于读取特征并执行推理的数据交易日；
  - `prediction_trade_date`：信号写入与下游消费交易日，固定为 `data_trade_date` 的下一个交易日（明日）。
- 模型维度不再固定 48：后端按 `metadata.json` / `feature_schema.json` / `input_spec.tensor_shape` 动态解析期望维度（解析失败才回退 `INFERENCE_DEFAULT_FEATURE_DIM`）。
- 维度门禁查询已适配 `market_data_daily` 多种字段形态：支持 `features(JSONB array)` 与 `feature_*` 列自动探测；若仅存在其中一种会自动降级，不再因缺少另一种列触发 SQL 异常阻断。
- “覆盖数阈值”改为自适应：综合 `INFERENCE_MIN_READY_SYMBOLS`（绝对上限）、`INFERENCE_MIN_READY_RATIO`（比例阈值，默认 0.9）、`INFERENCE_MIN_READY_FLOOR`（最低下限，默认 100）计算。
- 推理结果保留策略：`engine_signal_scores/engine_feature_runs` 的 `inference_script` 数据默认仅保留最近 30 天（可通过 `INFERENCE_PREDICTION_RETENTION_DAYS` 调整）。
- 新增管理员预测查询接口：
  - `GET /api/v1/admin/models/predictions`：按预测日/租户/用户/run_id 查询预测批次；
  - `GET /api/v1/admin/models/predictions/{run_id}`：查看批次内 symbol 分数明细。
- “明日信号生成”触发入口统一为两种：管理员手动触发 `POST /api/v1/admin/models/run-inference` 与 Celery Beat 08:55 自动兜底（`engine.tasks.auto_inference_if_needed`）。
- `run-inference` 返回体新增标准字段：`fallback_used`、`fallback_reason`、`failure_stage`，用于标记是否走 alpha158 兜底及失败阶段。
- `run-inference` 返回体补充字段：`active_model_id`、`active_data_source`，用于标记本次实际生效模型与数据源（`model_qlib/db/qlib_data` 或 `alpha158/db/Alpha158_bin`）。
- 管理仪表盘 `GET /api/v1/admin/dashboard/metrics` 在空库/缺表场景下会自动降级为 0 指标，避免后台页面出现 500 与连带的浏览器 CORS 误报。
- 新增特征字典接口：`GET /api/v1/admin/models/feature-catalog`，优先读取 `qm_feature_set_*` 注册表（`qm_feature_category/qm_feature_definition/qm_feature_set_version/qm_feature_set_item`），若注册表不可用则回退 `config/features/model_training_feature_catalog_v1.json`。
- 短信发送失败错误语义增强：`/api/v1/sms/send` 与 `/api/v1/users/me/phone/send-code` 在短信 SDK/配置缺失时返回 `503` 且给出明确错误（如“短信服务依赖未安装”），避免统一 400 导致排障困难。
- 生产镜像依赖补齐：`requirements/production.txt` 已纳入阿里云短信 SDK（`alibabacloud_dysmsapi20170525`、`alibabacloud_tea_openapi`、`alibabacloud_tea_util`），避免容器内短信功能因依赖缺失固定返回 `503`。
- 新增登录历史接口：`GET /api/v1/users/{user_id}/login-history`，支持 `page/page_size/success/login_type/start_date/end_date` 查询参数，返回个人中心登录记录所需字段（状态、类型、设备、IP、时间、失败原因）。
- 用户档案表 `user_profiles` 新增字段 `ai_ide_api_key`（TEXT，可空）用于保存用户级 AI-IDE API Key；服务启动后会幂等执行补列（`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`），兼容历史数据库。
- 管理端推理交易日历接入（2026-04-07）：
  - `GET /api/v1/admin/models/precheck-inference` 与 `POST /api/v1/admin/models/run-inference` 已改为通过交易日历中心解析 `data_trade_date/prediction_trade_date`；
  - 返回新增 `requested_inference_date`、`calendar_adjusted`，用于标记候选日期是否被自动校正为最近交易日。

## 管理端训练任务更新（2026-04-03）

- `backend/services/api/routers/admin/admin_training.py` 已继续拆分为：
  - `admin_training_utils.py`：请求规范化、特征校验、结果聚合、训练提交/回调辅助逻辑
  - `admin_training.py`：管理员训练相关路由与兼容入口
- 拆分后补齐了 `admin_training.py` 对 `admin_training_utils.py` 的显式私有符号导入（`_resolve_admin_scope`、`_SetDefaultModelRequest`、`_SetStrategyBindingRequest`），避免 `import *` 在 Python 中跳过前导下划线名称导致的启动期/运行期 `NameError`。
- `routers/admin/model_management.py` 继续对 `model_training.py` 兼容再导出特征目录相关 helper（`_enrich_feature_catalog_with_data_coverage`、`_load_feature_catalog_from_db`、`_load_feature_catalog_from_file`），避免拆分后旧调用链失效。
- `POST /api/v1/admin/models/run-training` 新增请求参数规范化与校验：
  - 基础字段：`train_start/train_end/features/model_type/num_boost_round/val_ratio/lgb_params`；
  - 显式切分字段（可选）：`valid_start/valid_end/test_start/test_end`；
  - 可解释性字段（可选）：`explain(enable_shap/shap_split/shap_sample_rows)`，默认开启 SHAP 汇总；
  - 产物要求（可选）：`required_artifacts`（默认 `model.bin/pred.pkl/metadata.json/result.json`）。
- 当传入显式切分字段时，后端会强制校验时间顺序：
  `train_start <= train_end < valid_start <= valid_end < test_start <= test_end`。
- `run-training` 返回体新增 `payload`（规范化后的最终配置），便于前端与运维审计。
- 训练回调 `POST /api/v1/admin/models/training-runs/{run_id}/complete` 已改为日志追加模式，不再覆盖已有 Batch 轮询日志。
- 训练状态新增显式过渡态 `waiting_callback`（前端可直接展示），用于区分“Batch 已结束”与“最终回调未到”。
- 编排器超时兜底：`waiting_callback` 超过 `BATCH_WAITING_CALLBACK_TIMEOUT_SECONDS`（默认 600 秒）会自动转 `failed` 并记录超时原因。
- `run-training` 现增加特征白名单校验：`features` 必须属于当前生效特征字典（优先 `qm_feature_set_*`，不可用时回退 `config/features/model_training_feature_catalog_v1.json`），非法字段返回 `422`。
- `GET /api/v1/models/inference/latest` 现可读取交易侧当前生效推理批次（Redis `qm:signal:latest:{tenant_id}:{user_id}`），用于前端回显最新 `run_id` 并与当前模型做匹配检查。
- 训练页前端请求已按后端契约统一，不再提交旧字段 `selectedFeatures/timePeriods/params`，避免字段口径不一致导致训练任务使用默认参数或空特征。
- 训练状态查询 `GET /api/v1/models/training-runs/{run_id}`（及 admin 同名接口）已支持合并回测 Redis 实时日志快照：容器运行期的增量日志与进度优先从 Redis 读取，避免仅依赖 DB 尾日志导致“进度卡住”。

## 用户态模型训练闭环（2026-04-04）

- 新增用户态训练主入口（登录态可访问）：
  - `GET /api/v1/models/feature-catalog`
  - `POST /api/v1/models/run-training`
  - `GET /api/v1/models/training-runs/{run_id}`
  - `POST /api/v1/models/training-runs/{run_id}/complete`（内部回调别名）
- `GET /api/v1/models/feature-catalog` 与 `GET /api/v1/admin/models/feature-catalog` 返回体新增可选字段 `data_coverage`：
  - 来源：本地特征快照目录 `db/feature_snapshots/model_features_*.parquet`（路径可由 `TRAINING_LOCAL_DATA_PATH` 覆盖）；
  - 字段：`min_date/max_date/suggested_periods(train/val/test)`，用于前端训练页动态推荐默认时间窗，避免请求落在无数据区间；
  - 结果会按 `FEATURE_COVERAGE_CACHE_TTL_SEC`（默认 300 秒）做内存缓存，降低重复扫描开销。
- 兼容策略：原 `admin` 命名空间训练路由继续保留，内部复用同一训练逻辑，避免历史调用中断。
- 训练任务查询已收敛为严格隔离：按 `tenant_id + user_id + run_id` 过滤；同租户不同用户不可互查任务日志与结果。
- `run-training` 请求契约新增并落盘字段：
  - `target_horizon_days`、`target_mode`
  - `label_formula`、`effective_trade_date`、`training_window`
  - `early_stopping_rounds`
  - `context(initial_capital/benchmark/commission_rate/slippage/deal_price)`
  - `explain(enable_shap/shap_split/shap_sample_rows)`（默认 `true/valid/30000`，`shap_sample_rows` 范围 `1000~100000`）
  - `feature_categories`、`generated_at`
- 训练编排器 `config.yaml` 已新增 `label/context/early_stopping_rounds` 配置，并将回调地址切换到用户态路径 `/api/v1/models/training-runs/{run_id}/complete`。
- 训练容器回收优化（2026-04-06）：在 `POST /api/v1/models/training-runs/{run_id}/complete` 回调落库后，API 侧会立即按容器名 `qm-train-{run_id}` 执行强制清理（`force + remove volumes`），避免训练完成后容器长期停留 `Exited`。
- 回调结果已统一规范化并校验为结构化契约：
  - `metrics(train/val/test -> rmse/auc)`
  - `artifacts(name + 可选 url/key)`
  - `summary(status/message)`
  - `metadata(目标口径、窗口、特征计数、benchmark、objective/metric 等)`
  - `error`
- 训练元数据现在会同时记录三层特征口径：`requested_feature_count/requested_features`（前端提交）、`auto_appended_feature_count/auto_appended_features`（训练脚本自动补齐的基础特征）、`feature_count/features/feature_columns`（最终实际入模维度）。
- 若回调标记 `completed` 但缺少关键字段（如 `metrics/artifacts/summary/metadata`），后端会自动降级为失败并返回明确错误，避免“伪成功”结果。

## 用户态模型推理中心（2026-04-04）
- 新增用户态推理主入口（登录态可访问）：
  - `GET /api/v1/models/system-models`
  - `GET /api/v1/models/inference/precheck`
  - `POST /api/v1/models/inference/run`
  - `GET /api/v1/models/inference/runs`
  - `GET /api/v1/models/inference/runs/{run_id}`
    - 返回推理批次摘要与信号排名明细；历史记录和详情均按 `tenant_id/user_id` 隔离
    - 若信号明细表暂时不可读，接口会降级返回摘要，避免前端展开直接 500
  - `GET /api/v1/models/inference/settings/{model_id}`
  - `PUT /api/v1/models/inference/settings/{model_id}`
- 推理前会先执行 precheck，校验模型目录、模型文件、`metadata.json`、推理数据目录、脚本文件、特征维度与当日数据覆盖情况；硬门禁失败会直接阻断执行。
- 推理结果统一落库到 `qm_model_inference_runs`，自动推理配置统一落库到 `qm_model_inference_settings`，支持按 `run_id / 日期 / 状态` 查询历史与追踪失败阶段。
- 推理结果返回体保留 `fallback_used`、`fallback_reason`、`active_model_id`、`effective_model_id`、`model_source`、`active_data_source`、`stdout`、`stderr`、`failure_stage` 等字段，便于前端追踪实际执行链路。
- 自动推理配置已服务端持久化；前端只负责读写和展示，不再依赖 `localStorage` 保存正式配置。
- `POST /api/v1/models/inference/run` 现会先完成用户态模型解析，再将 `resolved_model` 透传给引擎执行器，避免后台线程里再次发起异步 DB 解析导致 500。
- 管理员手动推理入口 `POST /api/v1/admin/models/run-inference` 也同步采用同样的 `resolved_model` 透传方式，避免 `run_in_executor` + `asyncio.run()` 的事件循环冲突。
- 推理返回值中若缺少 `model_source/effective_model_id`，API 会自动回退到已解析的模型上下文，保证返回契约稳定。
- `model_qlib` / LightGBM 推理脚本属于运行时依赖，模型推理 / 训练 / runner 统一使用 `quantmind-ml-runtime:latest`。

## 多用户模型注册与切换（2026-04-04）

- 新增用户模型注册表与策略绑定表：
  - `qm_user_models`
  - `qm_strategy_model_bindings`
- 训练回调在 `completed` 后自动触发模型注册与同步：
  - 源目录：`models/candidates/{run_id}/`
  - 目标目录：`models/users/{tenant_id}/{user_id}/{model_id}/`
  - 回调结果新增 `result.model_registration`（`model_id/status/error/storage_path/model_file`）。
- 新增用户态模型管理接口（登录态主入口）：
  - `GET /api/v1/models`
  - `GET /api/v1/models/default`
  - `PATCH /api/v1/models/default`
  - `GET /api/v1/models/{model_id}`
  - `GET /api/v1/models/{model_id}/shap-summary`（读取模型目录 `shap_summary.csv`，返回结构化 SHAP 因子贡献列表）
  - `POST /api/v1/models/{model_id}/archive`
- 新增用户态策略绑定接口：
  - `GET /api/v1/models/strategy-bindings/{strategy_id}`
  - `PUT /api/v1/models/strategy-bindings/{strategy_id}`
  - `DELETE /api/v1/models/strategy-bindings/{strategy_id}`
- 管理员兼容别名（复用同一 service，不新增并行逻辑）：
  - `GET /api/v1/admin/models/user-models`
  - `GET/PATCH /api/v1/admin/models/user-models/default`
  - `GET/POST /api/v1/admin/models/user-models/{model_id}[ /archive ]`
  - `GET/PUT/DELETE /api/v1/admin/models/user-models/strategy-bindings/{strategy_id}`
- 训练镜像回调契约更新为结构化对象（`metrics/artifacts/summary/metadata/error`），并继续兼容旧字段归一化。
- 新增迁移脚本：`scripts/migrate_user_model_registry.py`（建表+索引+存量用户 `model_qlib` 回填）。

## 社区关注关系更新（2026-03-09）
- 社区路由新增作者关注接口：
  - `GET /api/v1/community/authors/{author_id}/follow-status`
  - `POST /api/v1/community/authors/{author_id}/follow`
  - `DELETE /api/v1/community/authors/{author_id}/follow`
- 新增社区关注关系表 `community_author_follows`（租户隔离 + 唯一约束）用于持久化关注关系。
- 关注关系表在首次访问相关接口时执行幂等建表（`CREATE TABLE IF NOT EXISTS` + 索引创建），避免历史环境缺表导致接口失败。
- 社区审计写入降级：`write_audit_log` 改为 savepoint 内写入，审计落库异常仅记录告警并忽略，不再阻断点赞/评论/关注等主业务请求。
- 社区探索接口 `GET /api/v1/community/hot-users` 已扩展返回头像字段：
  - 新增字段：`id`、`avatar`（原有 `name/score/trend` 保持兼容）；
  - 社区首页“活跃达人”可直接消费该字段渲染真实头像。

## 通知中心契约更新（2026-03-09）

- `GET /api/v1/notifications` 现统一返回：
  - `{"code":200,"data":{"items":[...],"total":<总数>,"unread_count":<未读数>,"has_more":<是否有更多>}}`
  - 查询默认按 `tenant_id + user_id` 隔离，过滤 `expires_at` 已过期通知，并按 `created_at DESC` 排序。
  - 支持可选参数 `days`（`1-30`），用于仅返回最近 N 天通知；`total/unread_count` 也按同一时间窗口统计。
- `POST /api/v1/notifications/read-all` 返回结构统一为：
  - `{"code":200,"message":"...","data":{"count": <已标记条数>}}`
- 新增清空接口：
  - `POST /api/v1/notifications/clear`
  - 请求体：`{"days": 1-30}`（可选；不传表示清空当前用户全部未过期通知）
  - 返回：`{"code":200,"message":"...","data":{"count": <已清除条数>}}`
- 新增管理员公告接口（本期前端不曝光）：
  - `POST /api/v1/notifications/system-announcement`
  - 需管理员权限，支持指定 `user_id + tenant_id` 发布系统通知。
- 通知索引会在首次访问通知域时幂等补齐：
  - `(tenant_id, user_id, created_at DESC)`
  - `(tenant_id, user_id, is_read, created_at DESC)`
- 历史兼容：
  - 若旧库中存在 `notifications.is_read = NULL` 的历史数据，通知列表序列化会按 `false` 兼容处理。
  - 未读统计、单条已读、全部已读都会将 `NULL` 视为“未读”，避免旧数据触发 `500` 或未读数偏小。

## 股票搜索索引更新（2026-03-12）
- 新增网关本地股票搜索接口：`GET /api/v1/stocks/search`（`q`、`limit`）。
- 新增索引状态接口：`GET /api/v1/stocks/search/status`。
- 搜索数据源由服务器本地 JSON 索引提供（默认路径 `data/stocks/stocks_index.json`，可由 `STOCK_INDEX_JSON_PATH` 覆盖），避免前端直连第三方服务导致的 CORS 问题。
- 新增索引构建脚本：`backend/services/api/scripts/build_stock_index.py`，优先从 `stocks` 表抽取，若不存在则自动回退 `symbols` 表生成 JSON。
- 生产部署建议：每日收盘后执行一次构建脚本，确保新增/更名股票能被搜索命中。

示例命令：
```bash
source .venv/bin/activate
python backend/services/api/scripts/build_stock_index.py
```

## 实盘 API Key 路由修复（2026-03-12）
- `quantmind-api` 入口已补挂 `user_app` 的 API Key 路由：
  - `POST /api/v1/api-keys/init`
  - `GET /api/v1/api-keys`
  - `POST /api/v1/api-keys`
  - `PUT /api/v1/api-keys/{access_key}`
  - `DELETE /api/v1/api-keys/{access_key}`
- 修复前端实盘设置中心初始化凭证时出现的 `404 Not Found`（路由存在但未挂载到主应用）。

## QMT Agent 凭证接口（2026-03-14）
- `quantmind-api` 继续透出用户侧 API Key 能力，并新增 QMT Agent 双密钥辅助接口：
  - `POST /api/v1/api-keys/qmt-agent/bootstrap`
  - `POST /api/v1/api-keys/{access_key}/rotate-secret`
- `bootstrap` 为幂等接口：若当前用户已有默认交易 Key，则返回最新一条 `access_key`；仅首次创建时返回明文 `secret_key`。
- `rotate-secret` 会重置指定 `access_key` 的 `secret_key`，前端只在本次响应中展示一次。

## OpenClaw QuantBot 接入（2026-03-18）
- 新增 `backend/services/api/routers/quantbot_proxy.py`：QuantBot 适配代理路由，通过 `httpx` 将前端请求转发到 `COPAW_BASE_URL`。生产环境通过 `docker-compose.server.yml` 显式注入 `http://quantbot:8088`。
- 对外暴露 `/api/v1/openclaw/*` 共 11 个接口：`/chat`、`/push-messages`、`/sessions`（CRUD）、`/files/upload`、`/files`、`/files/{session_id}/{file_id}`、`/health`。
- `/api/v1/openclaw/chat` 当前直接透传 QuantBot 的 SSE 响应给前端；前端不再依赖短轮询窗口模拟”流式”。
- `/api/v1/openclaw/health` 当前固定返回 `components.api` 与 `components.quantbot` 两段状态，前端不再依赖兼容分支推断网关或上游状态。
- QuantBot 附件上传不走 COS，而是直接写入 `COPAW_SHARED_FILES_DIR` 指向的共享目录，并向 QuantBot 传递容器内可见路径 `COPAW_SHARED_VISIBLE_DIR/...`。
- 生产环境推荐将 `quantbot-data` volume 同时挂载到 `quantmind-api:/quantbot-shared` 与 `quantbot:/app/working`，保证 QuantBot 可直接读取用户上传的 PDF、Word、Excel、PPT、CSV、TXT、Markdown 等文件。
- 当前测试阶段启用范围：聊天、会话管理、历史消息、健康检查；不接入 cron jobs、模型、技能、工具、工作空间等管理能力。
- 测试阶段不配置上游访问密钥；如 QuantBot 后续增加鉴权，统一在 `quantbot_proxy.py` 适配，不由前端承担。
- 相关环境变量：`COPAW_BASE_URL`、`COPAW_CHANNEL`（默认 `console`）、`COPAW_TIMEOUT_SECONDS`（默认 `60`）、`COPAW_SHARED_FILES_DIR`（默认 `/copaw-shared`）、`COPAW_SHARED_VISIBLE_DIR`（默认 `/app/working`）、`OPENCLAW_MAX_FILE_SIZE_BYTES`（默认 `52428800`）。

## 管理端数据管理（2026-03-20）

- 新增管理员数据概览接口：`GET /api/v1/admin/models/data-status`。
- 新增管理员手动同步接口：`POST /api/v1/admin/models/sync-market-data-daily`（触发 `scripts/data/ingestion/sync_market_data_daily_from_baostock.py`，将 Baostock 基础行情回填到 `market_data_daily`）。
- 接口用于统一查看当前数据状态，返回两部分：
  - `qlib_data`：`db/qlib_data` 的日历范围、标的数量（SH/SZ/BJ）、特征目录数量、最新交易日覆盖统计（`at_target_count/older_count/invalid_count`）。
  - `market_data_daily`：数据库侧最新交易日、最新更新时间、当日行数、`feature_*` 列数量。
- 覆盖统计新增样本明细：`qlib_data.topn_samples`，包含 `older_samples` 与 `invalid_samples`（默认 Top20，可通过 `ADMIN_DATA_STATUS_SAMPLE_SIZE` 调整）。
- 该接口为“数据管理”页面提供后端数据源，便于运营快速判断“增量补数是否完成、数据是否新鲜、覆盖是否充足”。

## 交易日历中心（2026-04-07）

- 新增统一交易日历路由（登录态）：
  - `GET /api/v1/market-calendar/is-trading-day?market=SSE&date=2026-04-07`
  - `GET /api/v1/market-calendar/is-trading-time?market=SSE&dt=2026-04-07T14:35:00+08:00`
  - `GET /api/v1/market-calendar/next-trading-day?market=SSE&date=2026-04-07`
  - `GET /api/v1/market-calendar/prev-trading-day?market=SSE&date=2026-04-07`
  - `GET /api/v1/market-calendar/sessions?market=SSE&date=2026-04-07`
  - `POST /api/v1/market-calendar/batch-check`
- 数据隔离规则：
  - 所有查询均基于登录态 `tenant_id + user_id`；
  - 查询优先命中用户级覆盖，再命中租户级覆盖，最后回退到全局默认。
- 迁移脚本：
  - `backend/db/migrations/20260407_add_trading_calendar_center.sql`

## 模型推理交易日历接入（2026-04-07）

- 用户态模型推理接口已接入交易日历中心：
  - `GET /api/v1/models/inference/precheck`
  - `POST /api/v1/models/inference/run`
- 处理规则：
  - 输入 `inference_date` 为非交易日时，后端会自动回退到最近上一交易日作为 `data_trade_date`；
  - `prediction_trade_date` 继续按交易日历推导，不再依赖前端“仅跳过周末”的简化规则。
- 返回增强字段：
  - `requested_inference_date`：用户原始选择日期；
  - `calendar_adjusted`：是否发生交易日历自动校正。

## 测试
```bash
source .venv/bin/activate
pytest -q backend/services/tests/test_api_service.py
pytest -q backend/services/tests
```

- `routers/admin/model_management_ops.py` = 模型目录扫描、特征字典、数据状态、同步与推理前置检查
