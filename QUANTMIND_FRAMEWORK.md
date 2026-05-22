# QuantMind 完整框架文档

> 本文档用于 QuantBot 调用，实现本地操作和训练自动化。

---

## 一、项目概览

QuantMind 是一个量化交易平台，采用 Python FastAPI 后端 + Electron/React/TypeScript 前端。OSS 版本使用单容器 Docker 部署，所有后端服务运行在一个容器内。

**技术栈**: Python 3.10, FastAPI, PostgreSQL 15, Redis 7, React 18, Electron, Qlib, Celery

**部署方式**: Docker Compose 单镜像模式，所有服务共享一个容器进程

---

## 二、目录结构

```
PROJECT_ROOT/
├── docker-compose.yml          # Docker 编排文件
├── .env                        # 环境变量（部署时存在）
├── CLAUDE.md                   # 项目指引
│
├── backend/                    # Python 后端
│   ├── main_oss.py             # 统一入口：启动所有服务
│   ├── run_tests.py            # 测试运行器
│   ├── requirements.txt        # Python 依赖
│   │
│   ├── services/               # 四大服务实现
│   │   ├── api/                # API 网关服务 (:8000)
│   │   │   ├── routers/        # API 路由
│   │   │   │   ├── admin/      # 管理后台路由
│   │   │   │   │   ├── __init__.py          # 路由注册
│   │   │   │   │   ├── dashboard.py         # 仪表盘
│   │   │   │   │   ├── model_management.py  # 模型管理
│   │   │   │   │   ├── model_management_ops.py  # 数据管理操作（含全量同步）
│   │   │   │   │   ├── admin_training.py    # 管理训练
│   │   │   │   │   ├── strategy_templates.py # 策略模板
│   │   │   │   │   └── users.py             # 用户管理
│   │   │   │   ├── auth.py                  # 认证
│   │   │   │   ├── model_training.py        # 模型训练
│   │   │   │   ├── qwenpaw_proxy.py         # QwenPaw 代理
│   │   │   │   ├── asset.py                 # 资产
│   │   │   │   ├── profiles.py              # 用户画像
│   │   │   │   ├── research.py              # 研究
│   │   │   │   ├── community/               # 社区
│   │   │   │   └── ...
│   │   │   └── main.py                      # API 服务入口
│   │   │
│   │   ├── engine/             # 引擎服务 (:8001) - Qlib 回测
│   │   │   ├── tasks/          # Celery 任务
│   │   │   │   └── celery_tasks.py           # 回测/推理任务
│   │   │   ├── ai_strategy/    # AI 策略
│   │   │   └── ...
│   │   │
│   │   ├── trade/              # 交易服务 (:8002)
│   │   │   ├── runner/         # 交易执行
│   │   │   └── services/       # 交易服务
│   │   │
│   │   └── stream/             # 实时行情 (:8003)
│   │
│   └── shared/                 # 共享模块
│       ├── trading_calendar.py # 交易日历服务
│       ├── stock_utils.py      # 股票代码工具
│       ├── db_manager.py       # 数据库管理
│       ├── redis_client.py     # Redis 客户端
│       ├── config.py           # 配置
│       └── strategy_storage.py # 策略存储
│
├── electron/                   # Electron 前端
│   ├── src/
│   │   ├── features/quantbot/  # QuantBot 聊天功能
│   │   │   ├── pages/QuantBotPage.tsx   # 主页面
│   │   │   ├── components/              # 组件
│   │   │   └── services/agentApi.ts     # API 服务
│   │   └── ...
│   └── package.json
│
├── docker/
│   └── Dockerfile.oss          # OSS 镜像构建文件
│
├── scripts/                    # 运维脚本
│   ├── daily_update.py         # 每日数据更新
│   ├── eltdx_daily_update.py   # eltdx 数据更新
│   └── data/maintenance/
│       └── sync_stock_daily_full.py  # 日常全量同步（从 parquet 同步所有列）
│
├── db/                         # Qlib 数据目录
│   ├── custom/
│   │   └── fundamental_aligned.parquet  # 720万行×88列，数据源
│   ├── qlib_data/              # Qlib 格式数据
│   └── feature_snapshots/
│       ├── model_features_2024.parquet  # 186MB
│       ├── model_features_2025.parquet  # 188MB
│       └── model_features_2026.parquet  # 72MB
│
├── data/                       # 运行时数据
│   ├── 融资融券.json           # 融资融券股票池
│   └── ...
│
├── strategy_templates/         # 策略模板目录
│   ├── momentum_strategy.py
│   ├── momentum_strategy.json
│   └── ...                     # 11 个策略模板 (.py + .json 配对)
│
├── logs/                       # 日志目录
├── models/                     # AI 模型文件（按需下载）
├── user_pools_local/           # 用户股票池文件

注：RD-Agent 源码位于项目根目录 `./rd-agent/`，通过 Docker 挂载到容器内 `/app/rd-agent/`
```

---

## 三、Docker 部署架构

### 服务拓扑

```
docker-compose.yml 定义 5 个服务：
┌─────────────────────────────────────────────────────┐
│ quantmind (核心服务 - 单容器运行所有后端)              │
│   ├── API Gateway     :8000 (外部暴露)                │
│   ├── Engine (Qlib)   :8001 (外部暴露)                │
│   ├── Trade           :8002 (外部暴露)                │
│   ├── Stream          :8003 (外部暴露)                │
│   └── Celery Worker   (容器内进程)                    │
├─────────────────────────────────────────────────────┤
│ qwenpaw (聊天机器人 - agentscope/qwenpaw:latest)     │
│   ├── 内部端口 :8088                                 │
│   ├── 外部端口 127.0.0.1:8089:8088                   │
│   ├── 网络别名: copaw                                │
│   ├── 已挂载 QuantMind 代码库（只读）                  │
│   ├── 已挂载 QuantMind 数据（读写）                    │
│   ├── 已挂载 RD-Agent 源码（只读）                     │
│   └── 已挂载 Docker Socket                           │
├─────────────────────────────────────────────────────┤
│ db (PostgreSQL 15) :5432                             │
│ redis (Redis 7)    :6379                             │
└─────────────────────────────────────────────────────┘
所有服务共享网络: quantmind-net
```

### 端口映射

| 端口 | 服务 | 说明 |
|------|------|------|
| 8000 | API Gateway | 用户认证、策略管理、社区、管理后台 |
| 8001 | Engine | Qlib 回测、AI 策略生成、模型推理 |
| 8002 | Trade | 订单管理、持仓、风控 |
| 8003 | Stream | 实时行情、WebSocket 推送 |
| 5432 | PostgreSQL | 数据库 |
| 6379 | Redis | 缓存/队列 |
| 8089 | QwenPaw (外部) | 聊天机器人 Web 界面 (127.0.0.1:8089) |

### 环境变量（关键）

```bash
# 数据库
DB_DRIVER=asyncpg
DB_HOST=db
DB_PORT=5432
DB_NAME=quantmind
DB_USER=quantmind
DB_PASSWORD=quantmind2026
DATABASE_URL=postgresql+asyncpg://quantmind:quantmind2026@db:5432/quantmind

# 远程数据源（增量同步用）
SOURCE_DATABASE_URL=postgresql://readonly_sync:quantmind_sync_2026@139.199.75.121:5432/quantmind

# Redis
REDIS_HOST=redis
REDIS_PORT=6379

# 安全
SECRET_KEY=cbe2c739fa3a59798800aede60f68b87205653395175097802931c12858c2c52
JWT_SECRET_KEY=同上

# 存储
STORAGE_MODE=local
STORAGE_ROOT=/data

# 服务间通信（单容器用 localhost）
TRADE_SERVICE_URL=http://127.0.0.1:8002
ENGINE_SERVICE_URL=http://127.0.0.1:8001
STREAM_SERVICE_URL=http://127.0.0.1:8003

# 模型训练（Docker 编排）
INTERNAL_CALL_SECRET=quantmind-internal-secret
TRAINING_IMAGE=quantmind-oss:latest
HOST_PROJECT_PATH=.

# QuantBot (QwenPaw 集成)
QWENPAW_BASE_URL=http://qwenpaw:8088
QWENPAW_SHARED_FILES_DIR=/qwenpaw-shared

# AI 模型 API Key
DASHSCOPE_API_KEY=mock-api-key-not-configured  # 用户需配置真实 key
QWEN_API_KEY=mock-api-key-not-configured
AI_IDE_LLM_BASE_URL=https://api.deepseek.com
AI_IDE_LLM_MODEL=deepseek-v4-pro
AI_IDE_LLM_API_KEY=mock-api-key-not-configured

# 配置
DEBUG=false
LOG_LEVEL=INFO
SERVICE_MODE=all
APP_EDITION=oss
```

### 卷挂载（quantmind 容器）

| 容器路径 | 主机路径 | 说明 |
|----------|----------|------|
| /data | ./data | 运行时数据（股票池等） |
| /app/models | ./models | AI 模型文件 |
| /app/db | ./db | Qlib 历史数据 + parquet 源数据 |
| /app/logs | ./logs | 日志 |
| /app/backend | ./backend | 后端代码（热更新） |
| /app/config | ./config | 配置文件 |
| /app/scripts | ./scripts | 运维脚本 |
| /app/strategy_templates | ./strategy_templates | 策略模板 |
| /app/user_pools_local | ./user_pools_local | 用户股票池 |
| /app/rd-agent | ./rd-agent | RD-Agent 源码 |
| /var/run/docker.sock | /var/run/docker.sock | 允许启动训练容器 |
| /qwenpaw-shared | qwenpaw-shared volume | QwenPaw 共享文件 |

### QwenPaw 容器额外挂载（已具备完整操作能力）

| 容器路径 | 权限 | 说明 |
|----------|------|------|
| `/app/backend` | 只读 | 后端 Python 代码库 |
| `/app/scripts` | 只读 | 运维脚本 |
| `/app/strategy_templates` | 只读 | 策略模板 |
| `/app/CLAUDE.md` | 只读 | 项目指引 |
| `/app/config` | 只读 | 配置文件 |
| `/app/rd-agent` | 只读 | RD-Agent 源码 |
| `/data` | 读写 | 运行时数据 |
| `/app/db` | 读写 | Qlib 数据 + parquet 源数据 |
| `/app/models` | 读写 | AI 模型文件 |
| `/app/logs` | 读写 | 日志 |
| `/app/user_pools_local` | 读写 | 用户股票池 |
| `/qwenpaw-shared` | 读写 | QwenPaw 共享目录 |
| `/var/run/docker.sock` | 读写 | 可启动训练容器 |

QwenPaw 容器还设置了与 quantmind 一致的数据库/Redis/API 环境变量，可通过 `httpx` 或 `docker exec` 直接操作。

---

## 四、数据库结构

### 核心表：stock_daily_latest

分区表，存储每日股票数据，是回测和策略的数据基础。

**列结构（89列）**：
- 主键: `trade_date` (date), `symbol` (text)
- OHLCV: `open`, `high`, `low`, `close`, `volume`, `amount`, `vwap`
- 涨跌停: `limit_up`, `limit_down`, `is_limit_up`, `is_limit_down`
- ST 标识: `is_st` (boolean)
- 指数成分: `idx_hs300`, `idx_zz500`, `idx_zz1000`, `idx_margin` (boolean)
- 技术指标: `ma5`, `ma10`, `ma20`, `macd`, `rsi`, `kdj_*`, `boll_*` 等
- 基本面: `pe`, `pb`, `total_mv`, `circ_mv` (市值单位：元)
- 其他: `turnover_rate`, `volume_ratio`, `amplitude`, `pct_change` 等
- 股票信息: `stock_name`, `industry`
- 趋势标识: `volume_trend_3d` (boolean)

**数据来源**: `fundamental_aligned.parquet` (720万行 × 88列，位于 `/app/db/custom/`)

### 其他关键表（共93张）

| 表名 | 用途 |
|------|------|
| users | 用户表（admin/普通用户） |
| portfolios | 投资组合 |
| strategies | 用户策略 |
| backtests | 回测记录 |
| engine_feature_runs | 引擎特征运行 |
| qlib_backtest_runs | Qlib 回测运行 |
| rd_agent_factors | RD-Agent 生成的因子 |
| trading_calendar | 交易日历自定义覆盖 |
| model_versions | 模型版本 |
| notifications | 通知 |
| ... | 共 93 张表 |

### 数据库初始化

初始化 SQL: `data/quantmind_init.sql` (399KB)

---

## 五、API 端点

### 管理后台 (`/api/v1/admin/`)

| 路径 | 方法 | 说明 |
|------|------|------|
| `/dashboard/stats` | GET | 仪表盘统计 |
| `/models/*` | 多种 | 模型管理 |
| `/data/sync-stock-daily-full` | POST | 日常全量同步（从 parquet 同步所有列到 DB） |
| `/data/qlib-update` | POST | Qlib 数据更新 |
| `/users/*` | 多种 | 用户管理 |
| `/strategy-templates/*` | 多种 | 策略模板管理 |

### 模型训练 (`/api/v1/model-training/`)

| 路径 | 方法 | 说明 |
|------|------|------|
| `/start` | POST | 启动训练 |
| `/status/{job_id}` | GET | 训练状态 |
| `/logs/{job_id}` | GET | 训练日志 |

### 引擎 (`/api/v1/engine/`)

| 路径 | 方法 | 说明 |
|------|------|------|
| `/backtest/start` | POST | 启动回测 |
| `/backtest/status/{job_id}` | GET | 回测状态 |
| `/inference` | POST | 模型推理 |
| `/ai-strategy/generate` | POST | AI 策略生成 |
| `/rd-agent/factor` | POST | RD-Agent 因子提交 |

### QuantBot / QwenPaw 代理 (`/api/v1/quantbot/`)

| 路径 | 方法 | 说明 |
|------|------|------|
| `/chat` | POST | 聊天请求 |
| `/sessions` | GET | 会话列表 |
| `/sessions` | POST | 创建会话 |
| `/sessions/{id}/messages` | GET | 会话消息 |

### 认证 (`/api/v1/auth/`)

| 路径 | 方法 | 说明 |
|------|------|------|
| `/login` | POST | 用户登录 |
| `/register` | POST | 用户注册 |
| `/profile` | GET | 用户信息 |

### 交易日历 (`/api/v1/market-calendar/`)

| 路径 | 方法 | 说明 |
|------|------|------|
| `/is-trading-day` | GET | 是否交易日 |
| `/trading-days` | GET | 交易日范围 |

---

## 六、数据流

### 数据更新流程

```
1. 本地 parquet 源数据
   ↓
2. fundamental_aligned.parquet (720万行 × 88列)
   ↓
3. sync_stock_daily_full.py (全量同步脚本)
   ├── 读取 parquet
   ├── 取最近 N 个交易日数据（默认 30 天）
   ├── 创建临时表（按 DB 列类型正确映射）
   ├── UPSERT 到 stock_daily_latest
   └── 验证结果
   ↓
4. stock_daily_latest 表（分区表，供回测/策略使用）
   ↓
5. Qlib 数据格式转换（daily_update.py）
   ↓
6. Qlib 回测 / AI 策略推理
```

### 全量同步脚本 (`sync_stock_daily_full.py`)

```
输入:
  - parquet 文件: /app/db/custom/fundamental_aligned.parquet
  - 环境变量: SYNC_MAX_DAYS (默认 30)
  - DB 连接: 从环境变量读取

处理:
  1. 读取 parquet，提取 trade_date 和 symbol
  2. 取最近 N 个交易日的唯一日期集合
  3. 过滤数据到目标日期
  4. 去重 (trade_date + symbol)
  5. 从 information_schema 获取 DB 列类型
  6. 取 parquet 与 DB 的列交集（排除主键）
  7. 创建临时表 _tmp_sdl_full_sync（按 DB 类型定义列）
  8. 插入数据到临时表（boolean 列特殊处理）
  9. UPSERT: INSERT ... ON CONFLICT (trade_date, symbol) DO UPDATE
  10. 验证最新交易日的数据完整性

输出:
  - success: true/false
  - stdout: 同步日志（最后 3000 字符）
  - stderr: 错误日志（最后 3000 字符）
  - exit_code: 进程退出码
```

### 训练流程

```
1. 前端发起训练请求 → /api/v1/model-training/start
   ↓
2. API 服务接收请求，写入 training_runs 表
   ↓
3. 通过 Docker Socket 启动训练容器
   ├── 使用 TRAINING_IMAGE (quantmind-oss:latest)
   ├── 挂载项目代码和数据
   ├── 设置训练参数
   └── 在 TRAINING_DOCKER_NETWORK 中运行
   ↓
4. 训练容器执行训练任务
   ├── 从 stock_daily_latest 读取特征数据
   ├── 训练模型
   ├── 保存模型到 /data 或 /app/models
   └── 更新训练状态
   ↓
5. 前端轮询 /api/v1/model-training/status/{job_id}
   ↓
6. 训练完成，模型可用
```

### 回测流程

```
1. 前端提交回测请求 → /api/v1/engine/backtest/start
   ↓
2. Engine 服务创建回测任务
   ↓
3. Celery Worker 执行回测
   ├── 使用 Qlib 框架
   ├── 从 stock_daily_latest 读取数据
   ├── 运行策略逻辑
   ├── 计算收益、风险指标
   └── 保存结果到 backtests 表
   ↓
4. 前端轮询状态
   ↓
5. 回测完成，查看结果
```

---

## 七、关键操作指南

### 1. 同步股票数据（全量）

```bash
# 通过 API 调用
curl -X POST "http://localhost:8000/api/v1/admin/data/sync-stock-daily-full?max_days=30" \
  -H "Authorization: Bearer <admin_jwt_token>" \
  -H "Content-Type: application/json"

# 直接在容器内运行
docker exec quantmind python /app/scripts/data/maintenance/sync_stock_daily_full.py
```

### 2. 更新 Qlib 数据

```bash
# 通过 API 调用
curl -X POST "http://localhost:8000/api/v1/admin/data/qlib-update" \
  -H "Authorization: Bearer <admin_jwt_token>"

# 直接运行
docker exec quantmind python /app/scripts/daily_update.py --force
docker exec quantmind python /app/scripts/eltdx_daily_update.py --force
```

### 3. 启动训练

```bash
curl -X POST "http://localhost:8000/api/v1/model-training/start" \
  -H "Authorization: Bearer <user_jwt_token>" \
  -H "Content-Type: application/json" \
  -d '{"strategy_id": "...", "params": {...}}'
```

### 4. 检查 API 健康状态

```bash
curl http://localhost:8000/api/v1/health
```

### 5. 重启服务

```bash
docker restart quantmind
docker restart quantmind-celery
```

---

## 八、QuantBot 调用接口

### QwenPaw 容器已具备的完整访问能力

QwenPaw 容器不再是一个孤立的沙箱，而是拥有对 QuantMind 系统的完整访问权限：

#### 文件系统（容器内路径）

| 路径 | 权限 | 说明 |
|------|------|------|
| `/app/backend` | 只读 | 后端所有 Python 源码 |
| `/app/scripts` | 只读 | 运维脚本（同步、更新等） |
| `/app/strategy_templates` | 只读 | 11 个策略模板 (.py + .json) |
| `/app/CLAUDE.md` | 只读 | 项目配置指引 |
| `/app/rd-agent` | 只读 | RD-Agent 完整源码 |
| `/app/db/custom/fundamental_aligned.parquet` | 只读 | 720万行×88列源数据 |
| `/app/db/qlib_data/` | 只读 | Qlib 格式数据 |
| `/app/db/feature_snapshots/` | 只读 | 特征快照 parquet 文件 |
| `/data` | 读写 | 运行时数据（股票池、回测结果） |
| `/app/logs` | 读写 | 日志目录 |
| `/qwenpaw-shared` | 读写 | 共享文件目录 |

#### Docker 能力
- `/var/run/docker.sock` → 可启动/停止/管理 Docker 容器
- 可通过 `docker exec quantmind python ...` 在 quantmind 容器内执行任何 Python 脚本

#### 数据库
- PostgreSQL: `db:5432` (可通过 `psycopg2`/`asyncpg` 连接)
- Redis: `redis:6379` (可通过 `redis-py` 连接)

#### 网络
- 在 `quantmind-net` 网络中
- `quantmind:8000` (API Gateway)
- `quantmind:8001` (Engine)
- `quantmind:8002` (Trade)
- `quantmind:8003` (Stream)
- `db:5432` (PostgreSQL)
- `redis:6379` (Redis)

### QwenPaw Web 界面

访问: `http://<server_ip>:8089` 或容器内 `http://qwenpaw:8088`

### QwenPaw 代理 API（通过 QuantMind 后端）

```
POST /api/v1/quantbot/chat
Headers: Authorization: Bearer <jwt_token>
Body: {
  "message": "同步股票数据",
  "session_id": "可选，已有会话 ID"
}
```

### QwenPaw 共享文件目录

容器内路径: `/qwenpaw-shared`
主机路径: Docker volume `qwenpaw-shared`

QuantBot 可以通过读写此目录与 QuantMind 交换文件。

---

## 九、RD-Agent 集成

### 架构

```
QuantBot 聊天请求 → /api/v1/quantbot/chat
  → 意图识别 (factor_evolution)
    → RDAgentLauncher.launch_evolution()
      → python -m rdagent.app.qlib_rd_loop.factor  (直接调用 RD-Agent)
        → 生成因子 → 写入 rd_agent_factors 表 → Qlib 回测
```

### 安装状态

- **RD-Agent 版本**: 0.8.1 (editable install)
- **容器内路径**: `/app/rd-agent/`
- **Python 包**: `import rdagent` ✅ 可用
- **因子演化**: `from rdagent.app.qlib_rd_loop.factor import *` ✅ 可用
- **数据库表**: `rd_agent_factors` (自动创建)

### RD-Agent 操作方式

#### 方式 1: 通过 QuantBot 聊天接口（推荐）
QuantBot 自动识别意图，启动因子演化循环。

```
POST /api/v1/quantbot/chat
Body: {"message": "帮我挖掘一些高动量因子的 alpha 因子", "history": []}
```

#### 方式 2: 通过 API 直接提交因子
```bash
curl -X POST "http://quantmind:8001/api/v1/rd-agent/factor" \
  -H "X-Internal-Call: quantmind-internal-secret" \
  -H "Content-Type: application/json" \
  -d '{"factor_name": "test", "factor_code": "import pandas as pd\ndef compute(df): return df[\"$close\"].rolling(20).mean()", "signal": "$close"}'
```

#### 方式 3: 通过 QwenPaw 直接执行脚本
QwenPaw 可通过 Docker Socket 在 quantmind 容器内执行命令：
```bash
docker exec quantmind python -m rdagent.app.qlib_rd_loop.factor --base-features-path /path/to/seed.py --loop-n 5
```

#### 方式 4: 通过 API 查询因子结果
```bash
curl "http://quantmind:8001/api/v1/rd-agent/factor/{factor_id}"
```

### 数据库表结构

`rd_agent_factors` 表字段：
- `factor_id` (TEXT, PK)
- `factor_name` (TEXT)
- `factor_code` (TEXT)
- `status` (TEXT) - running/completed/failed
- `backtest_id` (TEXT)
- `created_at`, `completed_at` (TIMESTAMP)
- `ic_value`, `sharpe_ratio`, `annual_return`, `max_drawdown` (FLOAT)
- `error_message` (TEXT)
- `metadata` (JSON)

---

## 十、交易日历系统

### 支持的市场

| 市场代码 | exchange_calendars 名称 | 时区 | 交易时段 |
|---------|------------------------|------|---------|
| SSE | XSHG | Asia/Shanghai | 09:30-15:00 |
| SZSE | XSHG | Asia/Shanghai | 09:30-15:00 |
| CFFEX | XSHG | Asia/Shanghai | 09:30-15:00 |
| XNYS | XNYS | America/New_York | 09:30-16:00 |
| XNAS | XNAS | America/New_York | 09:30-16:00 |
| XHKG | XHKG | Asia/Hong_Kong | 09:30-16:00 (午休 12:00-13:00) |
| XTKS | XTKS | Asia/Tokyo | 09:00-15:00 (午休 11:30-12:30) |
| XKRX | XKRX | Asia/Seoul | 09:00-15:30 |

### 查询逻辑

1. 优先使用 `exchange_calendars` 作为主要日历源
2. DB 中的 `trading_calendar` 表作为自定义覆盖层（假期调整、临时休市）
3. `exchange_calendars` 不可用时 fallback 到 DB

### 使用方式

```python
from backend.shared.trading_calendar import calendar_service

# 检查是否交易日
is_trading = await calendar_service.is_trading_day("2026-05-22", market="SSE")

# 获取下一个交易日
next_day = await calendar_service.next_trading_day("2026-05-22", market="SSE")

# 获取日历
calendar = calendar_service.get_calendar_for_market("SSE")
```

---

## 十一、股票代码规范

- **强制格式**: 前缀式，如 `SH600036`
- **禁止格式**: 后缀式，如 `600036.SH`
- **自动识别**:
  - `SH`: 6xxxxx, 9xxxxx
  - `SZ`: 0xxxxx, 3xxxxx, 2xxxxx
  - `BJ`: 4xxxxx, 8xxxxx

### 工具函数

- 后端: `backend/shared/stock_utils.py` → `StockCodeUtil.to_prefix(code)`
- 前端: `electron/src/utils/portfolioUtils.ts` → `normalizeStockCode(code)`

---

## 十二、策略模板

位于 `strategy_templates/` 目录，包含 11 个策略模板，每个由 `.py` (策略代码) + `.json` (配置元数据) 组成。

策略通过 `/api/v1/admin/strategy-templates/` 管理。

---

## 十三、常用命令

```bash
# 启动所有服务
cd PROJECT_ROOT
docker-compose up -d

# 查看服务状态
docker-compose ps

# 查看日志
docker logs quantmind
docker logs quantmind-celery

# 进入容器
docker exec -it quantmind bash

# 重启服务
docker restart quantmind

# 重新构建镜像
docker-compose build

# 数据库连接
docker exec -it quantmind-db psql -U quantmind -d quantmind
```

---

## 十四、错误排查

### Celery Worker 崩溃

检查 `backend/services/engine/tasks/celery_tasks.py` 中 `await` 是否在 `async` 函数外使用。已修复：使用 `_run_async()` 包装异步调用。

### 全量同步 404

确保 `model_management_ops_router` 在 `backend/services/api/routers/admin/__init__.py` 中注册。

### 智能策略筛选返回 0 结果

检查 `stock_daily_latest` 表中 `is_st`, `idx_hs300`, `idx_zz1000`, `idx_margin` 等列是否有数据。如为 NULL/0，需运行全量同步。

### QuantBot 模型未配置

用户需在 QwenPaw Web 界面 (`http://<server>:8089`) 或 Electron 前端个人中心配置真实 API Key。当前为 mock key。

### RD-Agent 不可用

确认以下状态：
1. `docker exec quantmind python -c "import rdagent"` 应正常返回
2. `/app/rd-agent/.git` 存在（setuptools-scm 需要）
3. QwenPaw 能访问 `/app/rd-agent/` 目录
