# CLAUDE.md

本文件为 Claude Code（claude.ai/code）在此仓库中工作时提供指导。

## ⚠️ 免责声明

本项目仅供学习研究与技术演示，不构成任何投资建议。本系统产出的所有分析报告和交易信号均由 AI 自动生成，可能存在错误或偏差。投资决策请咨询持有中国证监会颁发资质的专业机构。作者不对使用本工具产生的任何投资损失承担责任。股市有风险，投资需谨慎。

## 项目概述

QuantMind 是一个量化交易平台，后端为 Python（FastAPI），前端为 Electron/React/TypeScript。开源版（OSS）采用单容器部署，所有后端服务运行在同一个容器中。

## 后端服务（统一入口 `backend/main_oss.py`）

| 服务 | 端口 | 职责 |
|------|------|------|
| api | 8000 | 用户认证、策略管理、社区、新闻代理 |
| engine | 8001 | Qlib 回测、AI 策略生成、模型推理、Alpha Agent |
| trade | 8002 | 订单管理、持仓、风控 |
| stream | 8003 | 实时行情、WebSocket 推送 |

## 常用命令

### 后端
```bash
# 启动全部服务（Docker）
docker compose up -d

# 本地运行单个服务
SERVICE_MODE=api python backend/main_oss.py

# 测试（在项目根目录执行）
python backend/run_tests.py unit        # 单元测试
python backend/run_tests.py integration # 集成测试
python backend/run_tests.py all         # 全部测试
python backend/run_tests.py trade-long-short  # QMT MVP 链路测试

# 代码检查与格式化
ruff check backend/
ruff format backend/
```

### 前端（Electron 应用，位于 `electron/`）
```bash
npm install              # 安装依赖
npm run dev              # 开发模式（Electron 桌面端）
npm run dev:web          # 开发模式（Web 浏览器）
npm run typecheck        # 类型检查
npm run dashboard:build  # 生产环境构建
```

## 架构要点

- **特征工程**：48 维特征由外部服务写入 `market_data_daily` 表
- **交易服务**：外部报单前强制「本地优先」落库持久化
- **Redis 库分配**：0=通用，1=认证，2=交易，3=行情，4=回测，5=缓存
- **共享模块**：`backend/shared/` 存放跨服务代码（DB 管理器、Redis 客户端、配置、日志）
- **瞬时时间（成交/委托）**：`sim_trades.executed_at` 等瞬时列一律 `TIMESTAMPTZ` + aware UTC。写入走 `backend/shared/utc_datetime.py` 的 `utc_now()` / `UtcDateTime`，JSON 输出带 `Z`。禁止 naive UTC 与上海墙钟混用。存量库由 `data/upgrade_v1.0.7.sql` 对齐。
- **策略存储**：`backend/shared/strategy_storage.py` 是所有策略增删改查的唯一入口
- **Celery worker 必须唯一**：`SERVICE_MODE=all` 下 `main_oss.py` 默认**不启动**内嵌 worker（需 `EMBEDDED_CELERY_WORKER=true`），消费队列的只有 `celery-worker` 容器。重复 worker 会瓜分 `qlib_backtest_srv` 队列消息，表现为定时任务随机「不执行」；排查看 `redis-cli client list | grep cmd=brpop` 应只有 1 个。
- **市场数据同步不内置默认调度**：是否开启、何时触发一律以用户在前端「同步调度」保存的 Redis 配置为准（`quantmind:sync_schedule:{market}`），未配置时 5 个市场全部 `enabled=false`；`MARKET_SUGGESTED_TIMES` 只是前端时间预填建议值（次日 00:00 以后错峰），不参与触发。详见 `backend/services/engine/README.md` →「定时调度与市场数据同步」。
- **Alpha Agent**：`backend/services/engine/alpha_agent/` - 因子演化启动器，经 RD-Agent 支持多市场
- **RD-Agent 集成**：`backend/services/engine/rd_agent/` - 封装微软 RD-Agent 的多市场因子挖掘框架
  - `market_adapters/` - MarketAdapter 模式：a_share（A股）、crypto（区块链）、hong_kong（港股）、us_stock（美股）
  - `rd_loop_wrapper.py` - 将 RD-Agent 的 FactorRDLoop 封装为 QuantMind 接口
  - `data_pipeline/` - 各市场专属的数据下载与格式转换
- **数据平台**：`backend/services/engine/data_platform/` - 多市场数据聚合（A股/港股/美股），适配不同数据源
  - **QuantDB 数据中枢**：`quantdb_hub.py` - 全部 A 股 parquet 读取的统一入口（DuckDB + pd.read_parquet）
  - **QuantDB 本地适配器**：`adapters/quantdb_local_adapter.py` - A 股主数据源（本地 parquet）
  - **QuantDB 远程适配器**：`adapters/quantdb_adapter.py` - 远程 SDK 实时查询（兜底）
  - **Qlib 数据构建器**：`qlib_data_builder.py` - 由 QuantDB parquet 生成 Qlib 二进制缓存（派生产物）
  - **字段路由**：`config/data_sources/field_routing.yaml` - quantdb_local 优先，旧适配器兜底
- **TradingAgents**：`backend/services/engine/trading_agents/` - 多 Agent A 股投研框架（7 个 AI 分析师、辩论、风险评估）
  - `runner.py` - TradingAgentsGraph 管线的后台线程运行器
  - `progress.py` - 线程安全的进度跟踪器（12 阶段）
  - `routers/trading_agents.py` - REST API（analyze、progress、report、history、download）
- **数据管线**：`backend/scripts/` - 统一的每日数据同步
  - `quantdb_daily_sync.py` - 主同步链路：sync_dataset() → parquet → PG 回填 → Qlib 缓存
  - `daily_data_sync.py` - 全量同步：QuantDB parquet → baostock → akshare → eltdx → PG → Qlib 缓存 → 指标 → parquet
  - `sync_investment_data.py` - 从 GitHub releases 下载并解压 qlib_bin（遗留兜底）
  - `update_feature_parquet.py` - 151 维特征计算（动量/波动率/流动性/资金流/风格）
- **新闻/RSS**：Huntly + RSSHub 聚合财经新闻，经 API 服务代理访问

## 股票代码标准化（分层口径，禁止跨层混用）

- **QuantDB parquet / Qlib / 行情数据层**：后缀式（如 `600036.SH`，Qlib 桥接用全小写 `sh600036`），否则静默查空
- **PG 数据库字段 / Redis 键 / 前端 / Strategy Lab SDK / 大多数 API**：前缀式（如 `SH600036`）
- **标准化工具**：
  - 后端：`backend/shared/stock_utils.py` → `StockCodeUtil.to_suffix(code)` / `.to_prefix(code)` / `.to_qlib(code)`
  - 前端：`electron/src/utils/portfolioUtils.ts` → `normalizeStockCode(code)`（输出前缀式）
- **市场自动识别**：
  - `SH`：6xxxxx、9xxxxx
  - `SZ`：0xxxxx、3xxxxx、2xxxxx
  - `BJ`：4xxxxx、8xxxxx
  - `HK`：5 位代码或 `.HK` 后缀
  - `US`：无数字前缀的股票代码（Ticker）

## 环境变量

必需的 `.env` 键（默认值见 `docker-compose.yml`）：
- `DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD`
- `REDIS_HOST`、`REDIS_PORT`
- `SECRET_KEY`、`JWT_SECRET_KEY`
- `STORAGE_MODE=local`（OSS 版必需）
- 本地数据目录（由 `docker-compose.yml` 设置，经 `./data:/data` 卷持久化，默认即容器内 `/data/xxx`）:
  - `QM_QUANTDB_DATA_DIR` - QuantDB A股本地 parquet（默认 `data/quantdb/`）
  - `QM_QUANTUS_DATA_DIR` - QuantUS 美股（默认 `data/quantus/`，Yahoo Finance 摄取）
  - `QM_QUANTHK_DATA_DIR` - QuantHK 港股（默认 `data/quanthk/`）
  - `QM_QUANTBC_DATA_DIR` - QuantBC 区块链/加密货币（默认 `data/quantbc/`，Binance 摄取；生产默认 `ENABLE_CRYPTO=false` 隐藏该市场）
  - `QM_QUANTFUTURES_DATA_DIR` - QuantFutures 期货/贵金属（默认 `data/quantfutures/`，akshare 摄取）
- 各市场的后台管理界面在「数据管理」页的 tab：QuantDB A股 / QuantUS 美股 / QuantHK 港股 / QuantBC 区块链 / QuantFutures 期货；同步入口为各 `backend/scripts/*_daily_sync.py`

## 代码风格

- Python：行宽 88，使用 ruff 做检查与格式化
- TypeScript：提交前端改动前必须运行 `npm run typecheck`

## 部署工作流

代码改动后务必：
1. **提交 git**：写描述清晰的提交信息
2. **部署到服务器**：在目标服务器上使用受控更新脚本

```bash
# 本地：提交改动
git add <changed-files>
git commit -m "descriptive message"
git push gitee master

# 注意：目标服务器的 SSH 别名/主机与项目目录因人而异，部署前先向用户询问确认。
# 用 ${SSH_TARGET} 和 ${PROJECT_DIR} 表示用户提供的具体值。
ssh ${SSH_TARGET} "cd ${PROJECT_DIR} && sudo bash deploy/update.sh"
```

Electron 前端在本地开发时使用 Vite HMR；修改 `electron/src` 后运行 `npm run typecheck` 即可，不需要复制构建产物到服务器的 `web` 容器。

### Web 前端部署（Nginx 预编译）
- **预编译目录**：`web/dist` 已纳入版本（`.gitignore` 放行 `!web/dist/**`），本地 `npm run dashboard:build` 后 `cp -r electron/dist-react/* web/dist/` 并提交，服务器 `git pull` 即更新，无需在服务器构建 `node`
- **服务**：`web` 容器 `nginx:alpine`（`docker-compose.yml:web`），挂载 `./web/dist:/usr/share/nginx/html:ro` + `./docker/web/nginx.conf:ro`，反代 `/api/ → quantmind:8000`、`/ws/ → quantmind:8003`，`resolver 127.0.0.11` 动态解析
- **更新**：`git pull && docker compose up -d web`（或 `restart web`），前端日常开发仍用 `npm run dev` HMR，无需每次重建
- **构建过滤**：`deploy/update.sh` 仅在 `requirements*.txt`/`Dockerfile` 变更时重建后端镜像，前端走 `web/dist` Volume，与后端构建解耦

## 关键文件

- `backend/main_oss.py` - 全部后端服务的统一入口
- `backend/run_tests.py` - 多模式测试运行器
- `backend/shared/` - 跨服务共享模块
- `minibt/` - vendored 裁剪版 minibt(20 行策略 DSL + 指标库 + 双引擎回测;补丁与口径差异见 `minibt/PATCHES.md`,运行时镜像 `docker/Dockerfile.minibt-runner`)
- `backend/shared/minibt_qdb.py` - QuantDB → minibt KLine 数据适配器(AI-IDE minibt 策略用)
- `backend/shared/minibt_result.py` - minibt 回测结果提取(修复浮盈/手续费口径,[RESULT] 行 + result.json)
- `backend/services/engine/routers/ai_ide/executor.py` - AI-IDE 代码执行器(minibt 自动路由 /execute/result 端点)
- `backend/services/engine/alpha_agent/launcher.py` - 因子演化启动器（支持 market 参数）
- `backend/services/engine/rd_agent/market_adapters/` - 市场适配器注册表（a_share、crypto、hong_kong、us_stock）
- `backend/services/engine/rd_agent/rd_loop_wrapper.py` - 桥接 RD-Agent 与 QuantMind 的 RDLoop 封装
- `backend/services/engine/routers/alpha_agent.py` - Alpha Agent API（含 /markets、带 market 参数的 /evolve）
- `backend/services/engine/routers/trading_agents.py` - TradingAgents REST API（analyze、progress、report、history）
- `backend/services/engine/trading_agents/runner.py` - TradingAgents 后台线程运行器
- `backend/services/engine/trading_agents/progress.py` - TradingAgents 进度跟踪器（12 阶段）
- `scripts/alpha_agent/run_rd_agent.py` - RD-Agent 多市场运行脚本（子进程入口）
- `backend/services/engine/data_platform/` - 多市场数据平台
- `backend/services/engine/data_platform/quantdb_hub.py` - QuantDB 数据中枢（A 股 parquet 读取统一入口）
- `backend/services/engine/qlib_data_builder.py` - 由 QuantDB parquet 构建 Qlib 二进制缓存
- `backend/services/engine/data_platform/adapters/quantdb_local_adapter.py` - A 股主数据适配器（本地 parquet）
- `backend/scripts/quantdb_daily_sync.py` - 主每日同步（sync_dataset → parquet → PG → Qlib 缓存）
- `backend/scripts/daily_data_sync.py` - 全量同步（QuantDB parquet → 多源兜底 → PG → Qlib 缓存 → 指标）
- `backend/scripts/sync_investment_data.py` - GitHub investment_data 下载与解压（遗留）
- `backend/scripts/update_feature_parquet.py` - 151 维特征 parquet 计算
- `backend/services/api/routers/admin/data_platform.py` - 数据平台管理端点（同步、parquet、健康）
- `backend/services/api/routers/news.py` - 新闻代理路由
- `backend/services/api/routers/market_kline.py` - K 线行情路由
- `electron/src/features/trading-agents/` - TradingAgents 前端模块（页面、组件、服务）
- `backend/shared/tdx_aidata/` - TdxAiData 实时行情通道（worker 唯一 SDK 引入口 + 订阅引擎写 `market:snapshot`/`market:series` 标准键，P6 盘中主源；**SDK 单次订阅上限 100 只**——超限整批拒绝且只打印不抛，>100 必须分片集群 `TdxAiDataCluster`，分片数在 Redis `qm:market:tdx_aidata:config.shard_count`）
- `backend/shared/l05_store.py` - L0.5 热集快照按日落盘（`data/l05_snapshots/date=YYYYMMDD/` + 质检 + 降冷 + 订阅写侧归档器，T-P6-04）
- `backend/shared/freshness.py` - 行情新鲜度分级唯一谓词（fresh/stale/unavailable；阈值 env 唯一读取点，T-P6-05）
- `backend/shared/feature_defs.py` - 批量特征定义唯一实现（151 维 OHLCV 因子；原 update_feature_parquet 纯迁移，改算法必须过金样）
- `backend/shared/feature_incremental.py` + `backend/services/engine/inference/incremental_features.py` - 增量特征引擎（32 列快车道与批量 ε=1e-9 金样锁定 + 快照状态机/分钟桶/provenance，T-P6-07）
- `backend/shared/market_regime.py` + `backend/services/engine/realtime_regime.py` - 市场状态（regime）口径单源（日频/日内共用）+ 日内服务（指数 live + 热集广度 → 快照/总线，T-P6-13）
- `backend/shared/intel_events.py` + `backend/services/stream/ws_core/intel_pusher.py` - 盘中情报总线（Redis Stream `intel:events`，schema 校验/生产消费 SDK/WS `intel.{tenant}.market.*` 鉴权，T-P6-11）
- `backend/services/engine/inference/realtime_{core,service}.py` + `replay_verifier.py` - 热集实时推理（ONNX、账本 `qm:realtime:infer:ledger:*`）与回放复现验收（同数据同特征同模型 → 信号 diff=0；CLI `scripts/p6_replay_verify.py`，T-P6-08/09）；**基线取数按模型 `metadata.data_source` 分派**（`load_baseline_for_model`：`quantdb_factors` → `QuantDBFactorReader` 直读最近可用因子日，其余 → 遗留 `model_features_{year}.parquet` 不可变快照；在线/回放共用，日级缓存）
- `backend/shared/latency_metrics.py` - 端到端时延打点（`intel:latency` 滚动统计；报表 `backend/scripts/latency_report.py`，T-P6-05）
- `backend/scripts/l05_maintenance.py` - L0.5 留存运维（质检报告 `report` / 容量 `capacity` / 降冷 `prune` 默认 dry-run）
- `backend/services/live_trading/services/qmt_quote_backup.py` - 大 QMT 备源行情席（全推订阅 → 主源陈旧超 30s 时旁路写 `market:snapshot/series`，source=qmt_big；状态 `qm:qmt:quote:backup:status`）
- `backend/services/live_trading/services/tdx_hot_set_feed.py` - TDX 桥热集行情轮询（trade 任务，env `TDX_HOTSET_FEED_ENABLED` 默认开）：热集 529 只含指数按 ~0.18s/只（桥限流 600/min 内）轮转取 `get_market_snapshot`（五档 Buyp/Buyv/Sellp/Sellv→bid1-5/ask1-5）→ 标准键 `market:snapshot/series`（source=tdx_bridge）；状态 `GET /tdx/quote-feed/status` → `hot_set` 段；节拍 `TDX_HOTSET_PACING_S` 可调
- `docker-compose.yml` - 本地部署配置
