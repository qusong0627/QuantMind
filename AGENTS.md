# QuantMind 开发指南（AGENTS.md）

本文件为 AI 编码助手（Claude Code / Codex / QwenPaw 等）在此仓库中工作时提供指导。

## 项目概述

QuantMind 是一个量化交易平台，后端为 Python（FastAPI），前端为 Electron/React/TypeScript。开源版（OSS）采用单容器部署，所有后端服务运行在同一个容器中。

## 后端服务（统一入口 `backend/main_oss.py`）

| 服务 | 端口 | 职责 |
|------|------|------|
| api | 8000 | 用户认证、策略管理、社区 |
| engine | 8001 | Qlib 回测、AI 策略生成、模型推理 |
| trade | 8002 | 订单管理、持仓、风控 |
| stream | 8003 | 实时行情、WebSocket 推送 |

## 常用命令

### 后端
```bash
# 启动全部服务（Docker）
docker-compose up -d

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
- **瞬时时间（成交/委托）**：`sim_trades.executed_at` 等瞬时列一律 `TIMESTAMPTZ` + aware UTC。写入走 `backend/shared/utc_datetime.py` 的 `utc_now()` / `UtcDateTime`，JSON 输出带 `Z`。禁止再把 naive UTC 和 Asia/Shanghai 墙钟混用，否则 asyncpg 会让整笔成交回滚。存量库由 `data/upgrade_v1.0.7.sql` 对齐。
- **策略存储**：`backend/shared/strategy_storage.py` 是所有策略增删改查的唯一入口
- **Celery worker 必须唯一**：`SERVICE_MODE=all` 下 `main_oss.py` 默认**不启动**内嵌 worker（需 `EMBEDDED_CELERY_WORKER=true`），消费队列的只有 `celery-worker` 容器。重复 worker 会瓜分 `qlib_backtest_srv` 队列消息，表现为定时任务随机「不执行」；排查看 `redis-cli client list | grep cmd=brpop` 应只有 1 个。
- **市场数据同步不内置默认调度**：是否开启、何时触发一律以用户在前端「同步调度」保存的 Redis 配置为准（`quantmind:sync_schedule:{market}`），未配置时 5 个市场全部 `enabled=false`；`MARKET_SUGGESTED_TIMES` 只是前端时间预填建议值（次日 00:00 以后错峰），不参与触发。详见 `backend/services/engine/README.md` →「定时调度与市场数据同步」。

## 股票代码标准化（重要，分层口径）

- **QuantDB parquet / Qlib / 行情数据层**：后缀式（如 `600036.SH`，Qlib 桥接用全小写 `sh600036`）。查 parquet、`stock-terminal`（仅接受后缀校验）、Qlib `D.features` 时必须用此格式，否则静默查空。
- **PG 数据库字段 / Redis 键 / 前端 / Strategy Lab SDK / 大多数 API**：前缀式（如 `SH600036`）。
- **层边界必须经 `StockCodeUtil` 显式转换，禁止散落手写切片**，禁止跨层混用：
  - 后端：`backend/shared/stock_utils.py` → `StockCodeUtil.to_suffix(code)` / `.to_prefix(code)` / `.to_qlib(code)`
  - 前端：`electron/src/utils/portfolioUtils.ts` → `normalizeStockCode(code)`（输出前缀式）
- **Redis 键格式**：
  - 快照：`market:snapshot:sh600036`（快照键用小写前缀）
  - 序列：`market:series:SH600036`（序列用标准前缀式）
- **市场自动识别**：
  - `SH`：6xxxxx、9xxxxx
  - `SZ`：0xxxxx、3xxxxx、2xxxxx
  - `BJ`：4xxxxx、8xxxxx

## 环境变量

必需的 `.env` 键（默认值见 `docker-compose.yml`）：
- `DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD`
- `REDIS_HOST`、`REDIS_PORT`
- `SECRET_KEY`、`JWT_SECRET_KEY`
- `STORAGE_MODE=local`（OSS 版必需）

## 代码风格

- Python：行宽 88，使用 ruff 做检查与格式化
- TypeScript：提交前端改动前必须运行 `npm run typecheck`

## 开发与部署工作流

### 1. 前端开发（NPM 模式）
- **本地开发模式**：前端统一使用本地 `npm run dev`（Electron 桌面端 / Vite Web 模式，自带 HMR 热重载）。
- **前端修改规则**：**修改前端（electron/src）代码后，不需要每次重新构建或重启服务器上的 `web` 容器**，本地可实时热重载预览调试。提交前运行 `npm run typecheck` 保证类型安全即可。

### 2. 后端同步与部署
- **后端修改规则**：**修改后端（backend/）代码后，必须推送到仓库并同步重启远程服务器上的后端容器**。

```bash
# 1. 本地提交并推送
git add .
git commit -m "descriptive message"
git push gitee NEXT

# 2. 同步并重启后端服务（服务名见 docker compose config --services）
# 注意：目标服务器的 SSH 别名/主机与项目目录因人而异，部署前先向用户询问确认。
# 用 ${SSH_TARGET} 和 ${PROJECT_DIR} 表示用户提供的具体值。
ssh ${SSH_TARGET} "cd ${PROJECT_DIR} && git pull && docker compose restart quantmind celery-worker celery-beat"
```

### 3. 镜像构建规则（是否需重新打包）
- **后端代码走 bind mount**（`./backend:/app/backend` 等挂载进容器），镜像只含 Python 依赖环境。
- **纯代码改动（未新增 pip 依赖、未改 Dockerfile/构建参数）**：只需 `git pull && docker compose restart`，**无需重新打包镜像**。
- **需要重build 的场景**：①新增了 `requirements.txt` 未收录的 Python 依赖；②升级 torch/qlib/duckdb 等底层库；③全新服务器首次部署无现成镜像。
- **重build 方式**（利用 Docker build cache，通常仅增量安装新增包）：服务器上执行 `docker compose build quantmind`，再 `docker compose up -d`。
- 本地 Windows 无法直接构建 linux/amd64 镜像，重build 一律在服务器或 CI 上进行。

### 4. QuantBot（dsh）技能与人格更新
- **技能：改仓库即生效**。技能经 `./skills` 只读挂载进 dsh 容器（容器内 `/root/.dsh/skills`），新增/修改 `skills/<name>/SKILL.md` 后 `docker compose restart dsh`，新会话即用；无上传/清单同步步骤，也不存在手工拷贝漂移问题。
- **人格与提示词**：`docker/dsh/dsh.cordis.yml`（persona/默认模型/连接）与 `docker/dsh/AGENTS.md`（路由表/平台 API/挂载地图/术语映射），改后 `docker compose restart dsh` 生效。
- 容器与端口：`quantmind-dsh`，宿主 8088（与旧 qwenpaw 相同，前端 QuantBot 页 iframe 直连）；外部用 IP/域名访问需 `.env` 配 `DSH_TRUSTED_HOSTS`。
- 排查：`docker compose logs -f dsh`；`docker exec quantmind-dsh ls /root/.dsh/skills | wc -l` 核对技能数。
- **旧 QwenPaw 为 legacy 备份（默认不启动）**：`docker compose --profile legacy up -d qwenpaw` 回滚（与 dsh 端口冲突，只能开一个）；其技能/人格更新入口仍是 `bash scripts/quantbot_init.sh`（仅 legacy 场景使用）。
- PDF 生成：dsh 容器不带 reportlab，统一走 `docker exec -w /app quantmind python3 backend/scripts/md_to_pdf_report.py`（技能契约首选路径）。

### 5. Web 前端部署（Nginx 预编译）
- **预编译目录**：`web/dist` 已纳入版本（`.gitignore` 放行 `!web/dist/**`），本地 `npm run dashboard:build` 后 `cp -r electron/dist-react/* web/dist/` 并提交，服务器 `git pull` 即更新，无需在服务器构建 `node`
- **服务**：`web` 容器 `nginx:alpine`（`docker-compose.yml:web`），挂载 `./web/dist:/usr/share/nginx/html:ro` + `./docker/web/nginx.conf:ro`，反代 `/api/ → quantmind:8000`、`/ws/ → quantmind:8003`，`resolver 127.0.0.11` 动态解析
- **更新**：`git pull && docker compose up -d web`（或 `restart web`），前端日常开发仍用 `npm run dev` HMR，无需每次重建
- **构建过滤**：`deploy/update.sh` 仅在 `requirements*.txt`/`Dockerfile` 变更时重建后端镜像，前端走 `web/dist` Volume，与后端构建解耦

## 关键文件

- `backend/main_oss.py` - 全部后端服务的统一入口
- `backend/run_tests.py` - 多模式测试运行器
- `backend/shared/` - 跨服务共享模块
- `docker-compose.yml` - 本地部署配置
- `scripts/quantbot_init.sh` - 【legacy】QwenPaw 技能/人格一键初始化（回滚场景专用）
- `docker/Dockerfile.dsh` - QuantBot 默认后端 dsh（DeepSeek Harness）镜像（node + dsh + nginx + docker CLI）
- `docker/dsh/` - dsh 配置：dsh.cordis.yml（persona/模型/连接）、AGENTS.md（工作区规则）、nginx.conf（前门+cookie 注入）、mint_cookies.py（鉴权 cookie 签发）、entrypoint.sh
- `docker/Dockerfile.qwenpaw` - 【legacy】QwenPaw 扩展镜像（reportlab + docker CLI）
