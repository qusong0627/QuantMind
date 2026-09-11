# quantmind-engine 计算引擎服务 V2

本服务是 QuantMind 的核心计算中枢，整合了 AI 策略生成、模型推理、高性能 Qlib 回测及深度投研分析功能。

## 修复记录（2026-03-27，自动推理任务稳定性）
- 修复 `InferenceScriptRunner._query_dimension_readiness` 的 `SessionLocal` 未定义异常，消除
  `NameError: name 'SessionLocal' is not defined`。
- 同步修复 `engine.tasks.auto_inference_if_needed` 的数据库预检查会话构造方式，统一使用同步 SQLAlchemy 会话（`+asyncpg -> +psycopg2`），避免
  `greenlet_spawn has not been called` 告警。

## 修复记录（2026-04-14，自动推理模型解析跨事件循环）
- 自动推理 Celery 路径已改为同步模型解析：`engine.tasks.auto_inference_if_needed` 与 `InferenceRouterService.run_daily_inference_script()` 统一使用 `ModelRegistryService.resolve_effective_model_sync()`。
- 目的：避免在 Celery worker 的后台线程/事件循环中重复调用 async SQLAlchemy session，修复
  `Task ... got Future ... attached to a different loop` 的模型解析失败。
- 影响：自动推理链路在解析用户默认模型、策略绑定模型和系统兜底模型时，不再依赖 `asyncio.run()`。

## 模型训练闭环契约（2026-04-04）
- 训练编排器 `LocalDockerOrchestrator._build_config_yaml` 已支持透传以下配置块：
  - `model.early_stopping_rounds`
  - `label.target_horizon_days/target_mode/label_formula/effective_trade_date/training_window`
  - `context.initial_capital/benchmark/commission_rate/slippage/deal_price`
- 训练链路阻断修复（2026-04-04）：
  - `docker/training/train.py` 已恢复完整训练主流程（调用 `train_model`，并回写 `model/metrics/metadata/result`），消除未定义变量导致的运行时失败；
  - `train.py` 新增 `data.source_mode + data.local_dir` 的本地快照读取能力，`LOCAL` 模式优先读取挂载目录，缺失时回退 COS；
  - `train.py` 新增分片空数据硬校验：当 `train/val/test` 任一分段为 0 行时，直接抛出可读错误（包含请求窗口、分片行数、可用数据范围），避免 LightGBM 底层 `num_data > 0` 断言报错；
  - 本地 Docker 训练编排器新增容器日志实时推送：运行中 stdout/stderr 增量写入回测 Redis Stream（默认 `quantmind-backtest-redis`），并同步维护任务状态快照（`pending/provisioning/running/waiting_callback/completed/failed`）；
  - 日志流可通过环境变量覆盖：`TRAINING_LOG_STREAM_ENABLED`、`TRAINING_LOG_REDIS_HOST/PORT/PASSWORD/DB`、`TRAINING_LOG_STREAM_PREFIX`、`TRAINING_LOG_STREAM_MAXLEN`、`TRAINING_LOG_STATE_TTL_SECONDS`；
  - `LocalDockerOrchestrator` 不再硬编码镜像 ID，优先读取 `TRAINING_IMAGE`；
  - 训练产物目录统一对齐用户模型注册路径：`models/users/{tenant_id}/{user_id}/{model_id}`。
- **模型训练数据泄漏防护 (2026-04-06)**:
  - **结构性泄漏防护**: `admin_training.py` 现强制要求在 `train/val` 和 `val/test` 分片之间保留不少于 `target_horizon_days` (预测跨度 $H$) 的交易日间隔。若检测到重叠（例如 $H=3$ 但间隔为 0），系统将阻断任务提交并提示合规建议。
  - **特征前瞻风险预警**: 修改了 `model_training_feature_catalog_v1.json`，针对标注为“当日值”的高频波动率特征（如 `vol_realized_rv`）增加了 **Look-ahead Bias** 风险提示。
  - **特征工程建议**: 强烈建议在回测或训练配置中使用 `Ref(feature_key, 1)` 处理高频当日指标，确保信号生成时仅使用历史已实现数据。
- 训练回调地址统一切换为用户态路径：`/api/v1/models/training-runs/{run_id}/complete`（admin 路径保留兼容别名）。
- 回调结果要求结构化返回：`metrics/artifacts/summary/metadata/error`；后端会在入库前校验关键字段并对不完整结果降级为失败，避免“作业成功但结果不可消费”。
- 训练容器回收修复（2026-04-06）：`LocalDockerOrchestrator` 在容器退出后不再固定等待整段 `TRAINING_CALLBACK_TIMEOUT_SECONDS` 才删除容器，而是按 `TRAINING_CALLBACK_CHECK_INTERVAL_SECONDS`（默认 2 秒）轮询任务状态；一旦回调将状态更新为 `completed/failed`，立即执行容器清理（`force=True, v=True`），避免训练面板长期堆积 `Exited` 容器。
- 训练预测产物修复（2026-04-06）：`docker/training/train.py` 生成的 `pred.pkl` 已从“仅验证集预测”调整为“覆盖 train/valid/test 全窗口预测”，并新增 `split` 字段标记分段；`metadata.json` 追加 `pred_coverage_start/pred_coverage_end/pred_rows`，用于回测覆盖面校验。

## AI-IDE 临时镜像验证闭环（2026-04-10）
- AI-IDE 执行器仍由 `quantmind-engine` 承载，挂载路径为 `/api/v1/ai-ide/execute`；真实运行默认使用 `AI_IDE_RUNNER_IMAGE`，未配置时回退到 `quantmind-ml-runtime:latest`。
- 前端可在“运行镜像（高级）”中显式填写临时 tag，后端会先执行 smoke 校验，再启动真实容器；未通过 smoke 时会直接阻断正式执行并在日志区回传失败阶段。
- 新增镜像 smoke 接口：`POST /api/v1/ai-ide/execute/smoke-image`，用于在正式运行前单独验证：
  - 镜像是否存在/可拉取
  - `python` 及关键依赖是否可导入
  - stdout/stderr 是否能稳定流式回传
- Smoke 容器默认采用更严格的沙箱参数：`--rm`、只读文件系统、`network_mode=none`、内存/CPU 限制、`cap_drop=["ALL"]`、`no-new-privileges`。
- 相关运行时环境变量：
  - `AI_IDE_RUNNER_IMAGE`
  - `AI_IDE_SMOKE_IMPORTS`
  - `AI_IDE_SMOKE_OPTIONAL_IMPORTS`
  - `AI_IDE_SMOKE_ALLOW_PULL`
  - `AI_IDE_SMOKE_CACHE_TTL_SECONDS`

## AI-IDE 助手开发规范闭环（2026-04-10）
- 右侧 AI 智能助手已切换为“先填入输入框、再补充需求、最后发送”的交互模式：选中代码不会直接发给大模型，而是先预填到输入框，便于用户补充目标、约束与修改范围。
- `/api/v1/ai/chat` 现在支持前端传入的 `extra_context.assistant_rules`，后端会将其作为开发规范上下文注入提示词，统一约束回答风格为：
  - 先结论后步骤；
  - 代码修改优先最小改动并明确文件路径；
  - 信息不足先提问，不做无依据假设；
  - 输出尽量简洁、可执行。
- 聊天请求还会附带 `file_path`，便于在大模型侧显式感知当前编辑文件，减少上下文歧义。

## AI-IDE 指标展示收口（2026-04-10）
- 右侧“关键指标”页已收窄为稳定核心项，主视图默认只展示总收益、年化收益、夏普比率、最大回撤、波动率以及交易统计。
- 基准收益、超额收益、贝塔、信息比率等参考指标在前后端均增加了合理性过滤；当数值异常或过大时会自动隐藏，避免把异常值直接展示到 UI。

## 模型推理中心闭环（2026-04-04）
- 用户态推理链路已从前端 mock 切换为真实后端执行：前置检查、手动推理、历史查询、结果明细和自动推理设置均由 `quantmind-api` 统一收口。
- 训练 / 推理 / 实盘 runner 当前统一使用 `quantmind-ml-runtime:latest` 运行镜像。
- 推理执行仍复用 `InferenceRouterService` / `InferenceScriptRunner`，并通过 `model_inference_persistence` 持久化到：
  - `qm_model_inference_runs`
  - `qm_model_inference_settings`
- 推理结果继续输出 `fallback_used/fallback_reason/active_model_id/effective_model_id/model_source/active_data_source/stdout/stderr/failure_stage`，用于前端展示与排障。
- `run_daily_inference_script(...)` 已补齐系统模型显式选择场景，支持 `models/production/*` 内部模型作为推理目标。
- 用户态 `/api/v1/models/inference/run` 现在会先在 API 主协程完成租户/模型解析，再把 `resolved_model` 传入推理执行器，避免在后台线程内重复 `asyncio.run()` 造成事件循环冲突。
- `model_inference_persistence.update_run()` 的 `result_json` 回写改为显式 JSONB 合并写法，避免 asyncpg 对 `CASE WHEN :result_json IS NULL` 产生参数类型歧义。

## 🚥 异步任务与参数优化 (Celery Worker)

`quantmind-engine` 使用 Celery 处理耗时的异步回测和复杂的参数优化任务。Worker 进程必须独立于主 API 服务启动。
当前已统一为 Celery 异步执行：`/api/v1/pipeline/*`、`/api/v1/qlib/backtest?async_mode=true`、`/api/v1/strategy-backtest-loop/*`。
API 进程仅负责入队或同步请求处理，不再保留 `BackgroundTasks` / `create_task` / 线程池式后台执行路径。
“明日信号生成”支持三种触发方式：管理员手动触发 `POST /api/v1/admin/models/run-inference`、策略激活后按当前 `tenant_id + user_id(8位)` 异步触发一次推理、以及 Celery Beat 00:00 自动兜底 `engine.tasks.auto_inference_if_needed`（周一至周五，见 `celery_config.beat_schedule`）。
生产部署必须同时运行 `celery-worker` 与 `celery-beat`；仅启动 worker 不会触发 00:00 自动推理。
如需因硬件压力临时暂停自动推理，可在运行环境设置 `AUTO_INFERENCE_ENABLED=false`（仅关闭 Beat 定时调度，不影响手动触发）。
推理链路已升级为“多用户模型解析 + 系统兜底”：
- 解析优先级：`显式 model_id > 策略绑定 > 用户默认 > model_qlib > alpha158`
- 用户模型目录：`models/users/{tenant_id}/{user_id}/{model_id}`
- 系统兜底目录：`model_qlib / alpha158`
四条入口统一输出：`fallback_used/fallback_reason/active_model_id/effective_model_id/model_source/active_data_source`；
`pipeline` 会将这些字段同时保存在 `inference_result` 和 `result_json` 顶层。

### 参数优化历史与恢复
- 网格搜索新增独立历史接口：
  - `GET /api/v1/qlib/optimization/history`
  - `GET /api/v1/qlib/optimization/{optimization_id}`
- 历史记录按 `user_id + tenant_id` 隔离，保留 `running/completed/failed/cancelled` 状态。
- `GET /api/v1/qlib/task/{task_id}/status` 已补充 `completed_count/failed_count/total_tasks/current_params/best_params/optimization_id`，便于前端恢复运行中优化任务。
- 网格参数优化请求现由后端统一限制组合数 `<=40`；超过上限的请求会在 API 入参校验阶段直接拒绝，避免绕过前端提交超大任务。
- 网格参数优化异步执行现为全局单槽位：同一时间仅 1 个优化任务可实际运行，其余提交保持 `pending/queued` 并等待前序任务释放执行锁。

### 1. 本地启动 (开发模式)
确保已激活虚拟环境并安装了所有依赖：

```bash
# 激活环境
source .venv/bin/activate

# 启动 Worker (推荐配置)
export PYTHONPATH=$PYTHONPATH:$(pwd)
python -m celery -A backend.services.engine.qlib_app.celery_config:celery_app \
  worker -Q qlib_backtest -l info --pool=prefork --autoscale=12,4
```

- `--pool=prefork`: 使用多进程池，适合 CPU 密集型的回测计算。
- `--autoscale=12,4`: 自动扩缩容，最少 4 个进程，最多 12 个（请根据机器核心数调整）。

### 2. Docker 启动 (生产推荐)
项目根 `docker-compose.yml` 已内置 `engine-worker` 服务。若需独立部署，可参考以下等价配置：

```yaml
  engine-worker:
    build:
      context: .
      dockerfile: backend/services/engine/Dockerfile
    container_name: quantmind-engine-worker
    command: python -m celery -A backend.services.engine.qlib_app.celery_config:celery_app worker -Q qlib_backtest -l info --pool=prefork --autoscale=8,2
    env_file:
      - backend/services/engine/.env
    volumes:
      - .:/app
      - ./db/qlib_data:/app/db/qlib_data
    environment:
      - PYTHONPATH=/app
      - DATABASE_URL=${DATABASE_URL}
    networks:
      - quantmind-network
    restart: unless-stopped
```

### 2.1 Worker 热更新要求
- `engine-worker` 是 Celery 长驻进程，且使用 `prefork` 子进程池。即使容器挂载了最新源码，已运行的 worker 子进程也可能继续持有旧模块。
- 当以下文件发生变更时，**不要只同步代码后继续跑异步回测**：
  - `backend/services/engine/qlib_app/services/strategy_builder.py`
  - `backend/services/engine/qlib_app/services/backtest_service.py`
  - `backend/services/engine/qlib_app/utils/extended_strategies.py`
  - `backend/services/engine/qlib_app/utils/margin_position.py`
  - 以及其他会影响策略构建/执行的 `qlib_app` 运行时代码
- 正确处理方式：

```bash
cd /home/quantmind
docker compose up -d engine-worker
```

- 上述命令会 recreate `engine-worker` 容器，让 Celery 重新导入最新代码；仅 `git pull`、仅重启 `engine-compute`、或仅在容器内手工 `python -c` 验证，都不能保证异步任务链路已更新。
- 本项目已实际出现过如下故障特征：
  - 前端/接口提交 `strategy_type=long_short_topk`
  - 但 `engine-worker` 日志中出现 `Unknown strategy type 'long_short_topk', falling back to TopkDropout`
  - 回测成交数异常偏低，且最终策略参数退化成普通 `TopkDropout`
- 建议每次发布后做一条最小烟测：
  - 提交 `long_short_topk` 异步回测
  - 检查 `engine-worker` 日志中是否出现 `Building LongShortTopK strategy`
  - 确认最终策略参数中存在 `short_topk/long_exposure/short_exposure`

### 3. 监控任务 (Flower)
可选：启动 Flower 仪表盘以可视化监控任务队列状态。

```bash
python -m celery -A backend.services.engine.qlib_app.celery_config:celery_app flower --port=5555
```

## 🚀 V2 版本核心增强 (2026-02-19)

### 1. 高保真交易模拟 (Precision Simulation)
- **非线性冲击成本模型**：升级了 `CnExchange`，引入**平方根定律 (Square Root Law)** 模拟大额资金对市场的冲击损耗。
- **精细化费率**：全面支持 A 股佣金（万分之二点五起）、印花税、过户费及最低收费限制。
- **数据预洗审计**：在回测启动前自动执行 `DataSanitizer` 审计，消除 `NaN` 坏点导致的计算异常。

### 2. 深度投研分析 (Advanced Analytics)
- **因子质量监控**：自动计算 **Rank IC**、**ICIR** 指标，量化模型预测效能。
- **五档分层收益**：提供收益单调性验证，识别模型是否过拟合。
- **风险风格归因**：实现 **Size/Value/Momentum/Volatility** 四维度暴露分析，穿透 Alpha 来源。

### 3. 实盘决策支持 (Actionable Insights)
- **调仓指令生成器**：自动对比回测理想持仓与当前状态，生成可执行的**篮子订单 (Basket Orders)**。
- **实时进度反馈**：通过 Redis/WebSocket 实现秒级进度推送，包含当前处理的交易日期。

## 🛠 架构组件
- **qlib_app**: 深度定制的本地化 Qlib 运行环境。
- **StyleAttributionService**: 负责风险暴露计算。
- **FactorAnalysisService**: 负责 IC/IR 及分层统计。
- **OrderGenerationService**: 负责理想权重向交易指令的转化。

### 核心修复与环境适配 (2026-02-20)
- **依赖隔离 (No Shadowing)**：本地模拟文件已重命名为 `qlib_mock.py`，彻底解决了单文件模块遮蔽正式 `pyqlib` 包导致的 `qlib is not a package` 错误。
- **Qlib 安装策略（2026-03-05）**：依赖统一锁定为 `pyqlib==0.9.7`（仅 `x86_64/AMD64` 平台安装）。镜像构建默认按架构处理：`amd64` 安装完整依赖，`arm64/aarch64` 自动跳过 `pyqlib`（因 PyPI 缺少 Linux ARM64 可用分发）；可通过 `SKIP_PYQLIB=0/1/auto` 显式覆盖。
- **ARM64 构建适配**：`Dockerfile` 保留 `cmake`、`swig`、`cython` 工具链，并新增按架构条件安装逻辑，避免 ARM64 场景因 `pyqlib` 安装失败导致镜像构建中断。
- **DuckDB 训练依赖（2026-04-02）**：生产依赖新增 `duckdb>=1.1.0`，用于云端特征快照与模型训练脚本的 SQL/Parquet 读取，避免训练容器运行时出现 `ModuleNotFoundError: duckdb`。
- **BatchCompute 训练链路修复（2026-04-03）**：`BatchOrchestrator` 已改为容器内通过 COS SDK 拉取 `config.yaml`（不再依赖 `InputMappings/OutputMappings`），并将轮询日志 `Limit` 调整为兼容值且允许日志拉取失败时继续写回作业状态；`train.py` 新增 `result.json` 直传 COS，确保失败/成功都可回溯。
- **BatchCompute 资源策略更新（2026-04-03）**：默认地域固定为广州五区（`ap-guangzhou-5`），默认机型为 `SA3.4XLARGE64`（16C/64G）；训练镜像默认 tag 更新为 `yaml-config-v2`。
- **BatchCompute 自动升配重试（2026-04-03）**：当作业失败原因为实例创建失败/机型 DryRun 失败等资源类错误时，编排器会自动按候选机型顺序重提同一 `run_id`（默认 `SA3.4XLARGE64 -> SA5.8XLARGE128 -> SA5.16XLARGE256`），并在训练任务日志中记录重试轨迹。
- **训练配置拉取稳定性（2026-04-03）**：训练镜像升级至 `yaml-config-v3`，`train.py` 新增 `--config-cos-key` 参数并内置 COS 下载逻辑，避免 Batch 内联 shell 命令导致的 `UserCommandError` 可观测性不足问题。
- **Batch 参数兼容增强（2026-04-03）**：训练镜像升级至 `yaml-config-v4`，`train.py` CLI 解析改为 `parse_known_args()`，可忽略 Batch 运行时注入的未知参数，避免参数解析直接退出。
- **Batch 配置契约增强（2026-04-03）**：`BatchOrchestrator` 新增统一 `config.yaml` 构建逻辑，支持显式切分 `split.train/valid/test`（优先于 `val_ratio`），并将 `required_artifacts` 透传至训练容器，便于产物一致性校验。
- **Batch 状态机增强（2026-04-03）**：Batch 作业 `SUCCEED` 后不再直接标记 `completed`，新增过渡态 `waiting_callback`，等待训练容器回调写入最终结果，避免“作业成功但结果未回写”被误判为完成。
- **Batch 回调超时兜底（2026-04-03）**：若 `waiting_callback` 超过 `BATCH_WAITING_CALLBACK_TIMEOUT_SECONDS`（默认 600 秒）仍未收到回调，任务自动置为 `failed`，并在日志/result 写入 `CALLBACK_TIMEOUT` 失败原因，避免任务长期卡住。
- **LLM 核心大脑**：已集成 **Qwen-Max** 主模型与 **Text-Embedding-V4** 向量模型，支持复杂的自然语言策略解析与向量语义路由。
- **云端同步增强**：已配置腾讯云 COS 存储，并启用自定义域名 **`https://cos.quantmind.cloud`**，实现策略文件的全球加速访问与品牌一致性。
- **内部身份校验加固**：`internal_auth_middleware` 统一校验 `X-Internal-Call` 与 `INTERNAL_CALL_SECRET`；所有 `/api/v1/*` 业务路由在缺失或密钥不合法时返回 `401`（`OPTIONS` 预检请求除外）。
- **身份来源统一**：`pipeline` 与 `qlib` 读写接口统一从 `request.state.user` 读取 `user_id/tenant_id`；Query/Body 中同名字段仅用于防伪校验，不再作为真实身份来源。
- **启动稳定性修复**：补齐 `Request` 导入，修复 `main.py` 在模块导入阶段的 `NameError` 风险。
- **AI 向导数据库池修复**：启动时显式注册 `shared.database_pool` 的 `postgres` 连接（兼容 `asyncpg -> psycopg2` URL 转换），修复 `query-pool` 报错“数据库 postgres 未注册”导致的 `500`。
- **Qlib 启动前置引导**：`engine/main.py` 生命周期增加 qlib bootstrap（`qlib_service.initialize + BacktestPersistence.ensure_tables`），确保直挂 qlib 路由时也完成运行时与表结构初始化。
- **高级分析路由挂载修复**：`engine/main.py` 已显式挂载 `qlib_app.api.analysis`，确保 `/api/v1/analysis/*` 在 `quantmind-engine` 主入口可用。
- **Qlib 数据目录默认值统一（2026-02-24）**：`qlib_app.main`、`qlib_app.tasks`、`pipeline_service`、`inference/config` 的默认 `QLIB_PROVIDER_URI` 统一为 `db/qlib_data`，避免遗留默认值 `research/data_adapter/qlib_data` 导致的数据目录偏移。

## 📦 策略管理规范
- **安全校验**：所有保存的策略强制通过 AST (抽象语法树) 扫描，禁止危险模块导入。
- **版本控制**：修改策略时自动在 `versions/` 目录备份旧版代码。
- **云端同步**：本地保存自动触发 **Tencent COS** 同步，确保个人中心跨设备可见。

## 🚦 启动与接口
- 默认端口：`8001`
- 核心入口：`POST /api/v1/pipeline/runs` (闭环流) 或 `POST /api/v1/backtest/qlib/backtest` (独立回测)
- 认证：通过 `quantmind-api` 统一鉴权，需携带有效的 `user_id` 和 `tenant_id`。
- CORS：开发/测试环境默认允许本机前端源；生产/预发环境必须显式配置 `CORS_ALLOWED_ORIGINS`（或 `CORS_ORIGINS`）白名单，通配符 `*` 会被拒绝。

## 三层融合规则配置（草案已落地）
- 配置文件：`backend/services/engine/config/fusion_rules.json`
- 可选环境变量：`ENGINE_FUSION_RULES_PATH`（覆盖默认路径）
- 配置结构：`layer1_lgbm`（日频候选池/权重）、`layer2_tft`（中周期筛选）、`layer3_risk_gate`（风险闸门）、`merge`（融合方式与权重）
- 当前运行语义（已接入回测）：`/api/v1/pipeline/runs` 在 `inference_enabled=true` 时，先生成 LGBM `pred.pkl`，再按 `fusion_rules` 执行「LGBM候选 -> TFT融合 -> 风控闸门」，产出 `fused_pred.pkl` 作为回测信号输入。
- 结果回传：`result_json` 新增 `fused_pred_path` 与 `fusion_report`，并继续回传 `pred_path`（原始 LGBM 产物）与 `fusion_rules` 快照，便于审计对比。
  - `fusion_report` 包含可消费标的清单：`selected_instruments`、`selected_scores`，可用于下游实盘发布端直接生成订单信号。
- 请求参数扩展：
  - `strategy_id`：用于 pipeline 推理阶段在未显式传 `model_id` 时按策略绑定解析生效模型；
  - `model_id`：改为可选；不传则自动按“策略绑定/用户默认/系统兜底”解析；
  - `tft_model_id`、`tft_inference_data`：用于第二层 TFT 推理（可选；缺失时按配置 fallback 回退）。
  - `risk_features`：用于第三层风控闸门（可选；格式为 `symbol -> {avg_turnover_20d, volatility_20d, industry}`）。
- 信号总线发布（P0 灰度）：
  - `ENABLE_SIGNAL_STREAM_PUBLISH=true` 开启将三层信号发布到 Redis Stream。
  - `SIGNAL_STREAM_PREFIX` 默认 `qm:signal:stream`。
  - `SIGNAL_LATEST_KEY_PREFIX` 默认 `qm:signal:latest`，用于标记某个 `tenant/user` 当前最新可消费的推理 `run_id`，供交易侧丢弃过期信号。
  - 可选独立发布库：`SIGNAL_STREAM_REDIS_HOST/PORT/DB/PASSWORD`。未设置时回退 `REDIS_HOST`；建议生产将信号流发布到与 runner 消费一致的 Redis（通常为 trade-redis）。
  - `SIGNAL_STREAM_MAXLEN` 默认 `200000`（近似裁剪）。
  - `pipeline` 现已支持双向策略参数透传：`enable_short_selling / margin_stock_pool / financing_rate / borrow_rate / max_short_exposure / max_leverage`。
  - 当 `enable_short_selling=true` 时，pipeline 发布到信号总线的负分信号会自动标记为 `sell_to_open + short`，正分信号标记为 `buy_to_open + long`。

## 模块边界（P1）
- `engine` 负责计算密集能力：策略生成、选股/解析、推理、回测、分析与报告。
- 主要路由归属：`/api/v1/strategies*`、`/api/v1/backtest/*`、`/api/v1/inference/*`、`/api/v1/pipeline/*`、`/api/v1/stocks/*`、`/api/v1/selection/*`、`/api/v1/wizard/*`。
- `market_data_daily`（推理输入）采用双通道兼容：
  - 显式 48 列：`feature_0 ~ feature_47`（便于人工补录与质量巡检）。
  - 历史 JSON：`features`（兼容旧链路）。
  - ETL 读取优先使用 `features`，若缺失则自动回退到 48 列组装。
- 实盘 E2E 最小契约接口（2026-03-02）：
  - `POST /api/v1/engine/runs/{run_id}/feature-ready`：标记特征批次完成（写 `engine_feature_runs`）。
  - `POST /api/v1/engine/runs/{run_id}/signal-ready`：写分数并标记 `signal_ready`（写 `engine_signal_scores`）。
  - `POST /api/v1/engine/dispatch/{batch_id}/stage`：更新分发批次状态机（写 `engine_dispatch_batches`）。
  - `POST /api/v1/engine/dispatch/{batch_id}/items/upsert`：按 `client_order_id` upsert 逐单状态（写 `engine_dispatch_items`）。
- 路径规范：`engine` 内统一使用 `backend.services.engine.ai_strategy.*`，不再使用历史 `backend.ai_strategy.*` 导入路径。
- 禁止范围：`engine` 不承担统一网关职责（认证入口/管理后台）与交易执行职责（订单/持仓/实盘）。
- P3 可观测性基线：统一注入并透传 `X-Request-ID` 响应头，便于跨服务链路追踪。
- P3 错误契约：统一错误结构 `error.code/error.message/error.request_id`，并兼容保留 `detail` 字段。
- P3 日志基线：统一访问日志字段 `service/request_id/tenant_id/user_id/method/path/status/duration_ms`。
- P3 指标基线：新增 `/metrics`（Prometheus），统一暴露 `quantmind_service_health_status{service="quantmind-engine"}` 与 `quantmind_service_degraded{service="quantmind-engine"}`。
- P3 健康语义：`/health` 在关键启动阶段异常（如初始化或 warmup 超时）时返回 `status=degraded`，与指标语义一致。
- P3 本地联调默认值：根 `.env` 可设置 `AI_STRATEGY_WARMUP=false`，避免开发/CI 场景因预热超时把服务误判为 `degraded`。
- P3 FastAPI 兼容：回测历史接口查询参数已改用 `pattern=`，避免 `regex=` 在新版本 FastAPI/Pydantic 下触发弃用告警。

## 🗓️ 定时调度与市场数据同步（2026-09-10）

### 1. 消费队列的 Celery Worker 必须唯一

- `backend/main_oss.py` 在 `SERVICE_MODE=all` 下**不再默认启动内嵌 Celery worker**，只有 `celery-worker` 容器消费队列。
- 需要单机调试、且没有独立 worker 容器时，才显式设置 `EMBEDDED_CELERY_WORKER=true` 打开内嵌 worker。
- **为什么**：内嵌 worker 与 `celery-worker` 连同一个 broker、消费同一个 `qlib_backtest_srv` 队列，会随机瓜分消息。表现为定时任务「有时不执行」，实际是跑在另一个 worker 上（日志与结果不在 `quantmind-celery` 里）；且内嵌 worker 是 `--pool=solo` 单进程，被长任务阻塞会拖垮 API 进程树（日志出现 `missed heartbeat` / `Substantial drift`）。
- **排查手法**：`redis-cli client list | grep cmd=brpop` 正常应只有 1 个；beat 每分钟发一次，对比 `quantmind-celery-beat` 的 `Sending due task` 数量与 `quantmind-celery` 的 `received` 数量应基本相等。

### 2. 市场数据同步：是否开启 / 何时触发一律以用户配置为准

- 配置入口：管理后台 → 数据平台 → 同步调度（`AdminQuantDBPanel`），API `POST /api/v1/admin/data-platform/sync-schedule/{market}`。
- 存储：Redis `quantmind:sync_schedule:{market}`；`celery-beat` 每分钟跑 `dispatch_market_sync` 比对派发 `run_market_scheduled_sync`。
- **任何市场都不内置默认启用**：未保存配置时 5 个市场（A / HK / US / BC / FUTURES）全部 `enabled=false`。
  原因：内置固定时刻会让所有部署在同一分钟全量同步，给上游数据源和服务器造成突发压力。
- `market_sync_scheduler.MARKET_SUGGESTED_TIMES` 只是**前端时间预填的建议值**，不参与触发；按既有约定统一放在次日 00:00 以后并互相错峰：
  `A 01:00 / HK 02:00 / FUTURES 03:00 / BC 04:15 / US 05:30`。
- 因此**新部署默认不会同步任何市场数据**，需要在前端显式开启；A 股不开则 `market_snapshot` 会持续 `skipped: stale`，推理质量回填也拿不到 T+5 真实收益。

### 3. 任务超时约束

- 全局：`task_time_limit=3600` / `task_soft_time_limit=3300`，`task_acks_late=true` + `task_reject_on_worker_lost=true`。
- `run_market_scheduled_sync` 单独收紧为 `soft 1800 / hard 2100`（`MARKET_SYNC_SOFT_TIME_LIMIT` / `MARKET_SYNC_TIME_LIMIT`）且 `acks_late=false`。
  原因：上游（如 yfinance）被限流时会把单个标的拖到分钟级，整体突破 3600s 硬限制 → 进程被 `SIGKILL`；叠加 `acks_late` 会重新入队再次超时，形成死循环。
- 数据拉取耗时超过预算一半时，`market_sync_scheduler.run_market_sync` 会跳过 Qlib 缓存重建并在结果里标记 `skipped`。

### 4. Redis（同时承载 broker / backend / 缓存）

- `docker-compose.yml` 中 Redis 必须 `--maxmemory-policy noeviction`：`allkeys-lru` 会在内存压力下静默淘汰队列消息，定时任务表现为「没跑」且无任何报错。
- 单次改动可用 `redis-cli config set maxmemory-policy noeviction` 热生效，无需重建容器。
