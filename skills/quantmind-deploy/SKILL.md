***

name: quantmind-deploy
description: "QuantMind 部署与运维 — 一键部署、快速部署、Docker 部署、数据库初始化、部署问题排查。在 QuantBot / Claude Code 中部署 QuantMind、排查部署失败、初始化数据库、检查服务健康、更新部署时使用。触发词：部署、一键部署、快速部署、部署失败、装不上、怎么部署、docker部署、部署问题、数据库初始化、服务起不来"
------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

> ## ⚙️ 运行环境契约（最高优先级，先于本文其余内容执行）
>
> 本技能可能运行在 **QuantBot（QwenPaw 容器）** 或**宿主机/本地 Claude Code**。执行前先探测环境（`which docker`、API 连通性），并遵守以下映射规则：
>
> 1. **后端 API 地址**：QwenPaw / 容器网络内一律用 `http://quantmind:8000`（`quantmind` 是 docker 网络别名）；仅宿主机调试用 `http://127.0.0.1:8000`。正文中出现的 `127.0.0.1:8000`、`localhost:800x`，在 QwenPaw 环境下自动替换为 `http://quantmind:8000`。
> 2. **取数脚本执行**：凡 import 了 `pandas / duckdb / psycopg2 / numpy / sqlalchemy` 等重依赖或 `backend` 包的脚本，**必须在 quantmind 容器内执行**（QwenPaw 本地 venv 无这些依赖）：
>
>    ```bash
>    docker cp <脚本路径> quantmind:/tmp/<脚本名> && docker exec -w /app quantmind python3 /tmp/<脚本名> <参数>
>    ```
>
>    脚本源三选一：宿主机 repo `skills/<name>/scripts/`、QwenPaw 工作区 `/app/working/workspaces/default/skills/<name>/scripts/`、挂载目录 `/quantmind/skills/<name>/scripts/`。纯标准库脚本（无重依赖）可在 QwenPaw 本地直接跑。
> 3. **报告落盘**：股票报告页可见的 MD/PDF 报告，直接写 `/data/reports/trading_agents/{市场或类别}/{股票名}/`（QwenPaw 对 `/app/db` 有写权限，**直接写文件，不要 docker cp**）；过程数据 facts 写 `/data/reports/<类别>/`（`/data` 可写）。
> 4. **MD → PDF 转换（按优先级降级）**：
>    ① `docker exec -w /app quantmind python3 backend/scripts/md_to_pdf_report.py <输入.md> <输出.pdf>`（研报级排版，首选）；
>    ② docker 不可用时，**改用 QwenPaw 内置** **`pdf`** **技能**把 MD 转成 PDF；
>    ③ 两者都不可用则只交付 MD，并明确告知用户 PDF 未能生成及原因。
> 5. 本文中的 `~/.claude`、`cp -r ... ~/.claude/skills` 等说明仅适用于本地 Claude Code 维护者，**QuantBot 不要执行**。

# QuantMind 部署技能

QuantMind 部署运维完整指南。覆盖**部署前准备 → 一键/手动部署 → 部署后检查 → 问题排查 → 更新 → 云端训练**全流程。本技能针对 AI 编程助手编写，每步都给出可直接执行的命令与判断标准，避免"不知道下一步"卡壳。

## 0. 安装技能包（让 AI 帮你部署）

本技能包兼容**主流 AI 编程工具**（Claude Code / Codex / OpenCode / Trae / MarsCode 等），安装后 AI 能自动识别"部署/装不上"等意图并调用本技能指导部署。

### 方式一：Claude Code / QuantBot（原生 SKILL.md）

```bash
# 解压到 Claude Code 全局技能目录
unzip quantmind-operations-skill.zip -d ~/.claude/
# 验证
ls ~/.claude/skills/quantmind-deploy/SKILL.md
```

### 方式二：从项目仓库安装（任何工具）

```bash
# 项目根目录 skills/ 下即全部技能
cp -r /opt/quantmind/skills/* ~/.claude/skills/
```

### 方式三：其他主流 AI 工具（OpenCode / Codex / Trae / MarsCode 等）

各工具虽不原生识别 SKILL.md，但都读取 **AGENTS.md**（项目级指令）。把技能包要点导入即可：

```bash
# ① 通用做法：把 SKILL.md 内容并入 AGENTS.md
# 项目根创建/追加 AGENTS.md，把关键流程粘贴进去
cat ~/.claude/skills/quantmind-deploy/SKILL.md >> AGENTS.md

# ② OpenAI Codex
unzip quantmind-operations-skill.zip -d ~/.codex/
# Codex 读取 ~/.codex/AGENTS.md（把本技能要点放入）

# ③ OpenCode
unzip quantmind-operations-skill.zip -d ~/.config/opencode/
# 或在项目根 AGENTS.md 引用本技能要点

# ④ 腾讯 Trae / 字节 MarsCode
# 克隆仓库后把 SKILL.md 要点写入项目 AGENTS.md，AI 即可按流程部署
```

### 让 AI 部署

安装技能后，直接对 AI 助手说：

- "帮我部署 QuantMind" → AI 读取本技能，按"部署前准备→部署→检查"执行

- "部署不上，帮我排查" → AI 按"问题排查"诊断树逐项定位

- "一键部署" → AI 执行 `quick-deploy.sh`

### 推荐编程工具（部署环境）

| 工具                        | 用途         | 说明                      |
| ------------------------- | ---------- | ----------------------- |
| **Claude Code**           | AI 编程/部署助手 | 原生支持 SKILL.md，装技能包即自动识别 |
| **OpenCode**              | AI 编程助手    | 开源，读 AGENTS.md          |
| **OpenAI Codex**          | AI 编程助手    | 读 \~/.codex/AGENTS.md   |
| **腾讯 Trae / 字节 MarsCode** | AI IDE     | 读项目 AGENTS.md           |
| **VS Code**               | 代码编辑       | 前端/后端调试                 |
| **Docker Desktop**        | 容器管理       | 本地调试用，服务器用 docker-ce    |
| **MobaXterm / Termius**   | SSH 终端     | 连服务器执行部署命令              |
| **Git**                   | 版本管理       | 拉取/更新代码                 |

## 架构总览

QuantMind 单机 Docker Compose 部署（`docker-compose.yml`），11+ 服务：

| 容器                       | 服务            | 端口        | 说明                          |
| ------------------------ | ------------- | --------- | --------------------------- |
| `quantmind-db`           | PostgreSQL 15 | 5432      | 主数据库                        |
| `quantmind-redis`        | Redis 7       | 6379      | 缓存/消息（DB 0-5 分配）            |
| `quantmind`              | 后端主服务         | 8000-8003 | api/engine/trade/stream 四合一 |
| `quantmind-celery`       | Celery Worker | —         | 异步任务（回测/同步/推理）              |
| `quantmind-celery-beat`  | Celery Beat   | —         | 定时调度                        |
| `quantmind-web`          | 前端 Web        | 80        | Nginx 托管 React 构建产物         |
| `quantmind-data-gateway` | 数据网关          | —         | 行情/资金流聚合                    |
| `quantmind-huntly`       | Huntly        | 8090      | RSS 新闻存储/阅读器                |
| `quantmind-rsshub`       | RSSHub        | 1200      | 通用网站订阅                      |
| `qwenpaw`                | QwenPaw       | —         | AI 代理（可选）                   |

## 1. 部署前准备（重要，先做完再部署）

### 1.1 环境要求（多系统 + 硬件）

| 项目          | 要求                           | 说明                                                      |
| ----------- | ---------------------------- | ------------------------------------------------------- |
| **系统**      | Ubuntu 22.04 LTS 或 24.04 LTS | **部署脚本仅支持 Ubuntu 22.04+**，其他系统会被拒绝                      |
| **Windows** | Docker Desktop + WSL2 后端     | 在 WSL2 终端内执行 `docker compose`                           |
| **macOS**   | Docker Desktop 直接运行          | 兼容                                                      |
| **云服务器**    | 任意 Docker 环境                 | 单机即可                                                    |
| **CPU 架构**  | **仅 x86\_64 / AMD64**        | **ARM（aarch64）不支持**——微软 Qlib 框架仅发布 x86\_64，ARM 无法装 Qlib |
| **CPU**     | 4 核以上                        | 推荐 8 核（训练/回测耗 CPU）                                      |
| **内存**      | 16GB 以上（运行）                  | 模型训练推荐 **64GB+**，推理/回测 **32GB+**；内存不足会 OOM 训练卡死         |
| **磁盘**      | 100GB 以上可用                   | 数据 + 镜像 + 特征快照（\~15GB）+ 模型                              |

> ⚠️ 训练机建议 ≥64GB 内存；若只有 32GB，缩小时间窗/特征数避免 OOM。

### 1.2 网络检查（部署前必测）

国内服务器常需配置镜像源。部署脚本会自动选 Docker/PyPI/APT 镜像源，也可手动指定。

```bash
# 检查 DNS + 各仓库连通性（任一失败先解决再部署）
curl -fsSL --connect-timeout 5 https://gitee.com >/dev/null && echo "gitee OK" || echo "gitee FAIL"
curl -fsSL --connect-timeout 5 https://registry.npmmirror.com >/dev/null && echo "npm OK" || echo "npm FAIL"
curl -fsSL --connect-timeout 5 https://pypi.org >/dev/null && echo "pypi OK" || echo "pypi FAIL"
docker info >/dev/null 2>&1 && echo "docker OK" || echo "docker 未安装（部署脚本会装）"
```

**网络差时的应对**：

- 手动指定镜像源：`QUANTMIND_MIRROR=aliyun sudo bash deploy.sh`（或 `tuna`/`huaweicloud`）

- Docker 镜像源：`/etc/docker/daemon.json` 配置 `registry-mirrors`（阿里云/腾讯云镜像加速）

- PyPI 源：`PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/`（构建镜像时 `--build-arg`）

### 1.3 提前决定要填写的内容

部署脚本会**交互式**问你以下内容，提前准备好答案：

| 部署时问什么              | 示例答案                          | 说明                                |
| ------------------- | ----------------------------- | --------------------------------- |
| 服务器 IP              | `192.168.1.100` / `localhost` | 无公网 IP 用 localhost 或局域网 IP        |
| 选择镜像源               | 国内选阿里云/中科大                    | 网络差时自动选，也可 `QUANTMIND_MIRROR=` 指定 |
| 是否确认部署              | `y`                           | 确认后开始安装                           |
| （可选）QuantDB API Key | `qdb_xxx`                     | **部署后**在后台填，见 \[\[quantdb-sdk]]   |

### 1.4 部署前备份

```bash
# 若已有旧数据/旧部署，先备份
sudo cp -r /opt/quantmind/data /opt/quantmind/data.bak.$(date +%Y%m%d)
```

## 2. 一键部署（推荐）

```bash
# 需要 root 权限；默认固定到发布 tag v1.9.0-beta（可复现、可校验）
curl -fsSL https://gitee.com/qusong0627/QuantMind/raw/v1.9.0-beta/deploy/quick-deploy.sh | sudo bash

# 使用最新 master（不推荐生产）
QUANTMIND_DEPLOY_TAG=master curl -fsSL https://gitee.com/qusong0627/QuantMind/raw/master/deploy/quick-deploy.sh | sudo bash

# 校验 deploy.sh 完整性（生产建议设置 SHA256）
QUANTMIND_DEPLOY_SHA256=<sha256> QUANTMIND_DEPLOY_TAG=v1.9.0-beta sudo bash quick-deploy.sh
```

**部署 6 阶段**：

1. **系统准备**：更新依赖、装 Docker & Compose v2.19+、Node 20、Nginx
2. **代码部署**：从 Gitee 克隆到 `/opt/quantmind`、配置 `.env`、创建数据目录
3. **后端部署**：构建 Docker 镜像、启动 PG/Redis/QuantMind、**执行** **`db_init.sql`** **初始化数据库**
4. **前端部署**：npm 依赖 + 构建 + PM2 启动
5. **Nginx 配置**：反向代理
6. **验证**：健康检查 + 防火墙

## 3. 快速部署（已下载脚本）

```bash
sudo bash deploy/quick-deploy.sh
# 指定服务器 IP（公网/局域网/localhost 自动检测）
sudo bash deploy/deploy.sh localhost
sudo bash deploy/deploy.sh 192.168.1.100
QUANTMIND_SERVER_IP=192.168.1.100 sudo bash deploy/deploy.sh
```

## 4. 手动部署

```bash
sudo git clone https://gitee.com/qusong0627/QuantMind.git /opt/quantmind
cd /opt/quantmind
sudo chmod +x deploy/deploy.sh
sudo ./deploy/deploy.sh
```

## 5. 数据库初始化（关键）

**deploy.sh 自动执行** **`backend/shared/db_init.sql`**（含 users 等全部核心表）：

- 容器内路径 `/app/backend/shared/db_init.sql`（由 `./backend:/app/backend` 挂载提供）

- 优先从 quantmind 容器执行 psql，失败则从 db 容器执行

- 若存在 `data/quantmind_init.sql` 则补充初始化数据

```bash
# 手动执行数据库初始化
docker exec quantmind bash -c "psql -h db -U quantmind -d quantmind -f /app/backend/shared/db_init.sql --quiet -v ON_ERROR_STOP=0"
```

**初始化后必须验证 users 表存在**（最常见部署失败点）：

```bash
docker exec quantmind-db psql -U quantmind -d quantmind -c "\dt users"
# 期望看到 users 表；若不存在说明 db_init.sql 没跑成功
```

## 6. 部署后检查（按顺序，每步都过再继续）

### 6.1 容器健康

```bash
docker compose -f /opt/quantmind/docker-compose.yml ps
# 期望 quantmind/quantmind-celery/quantmind-celery-beat/quantmind-web/quantmind-db/quantmind-redis 都 Up
```

### 6.2 数据库 & Redis

```bash
docker exec quantmind-db pg_isready -U quantmind          # 期望 "accepting connections"
docker exec quantmind-redis redis-cli ping                # 期望 "PONG"
```

### 6.3 后端 API 健康

```bash
curl -s http://localhost:8000/api/v1/health
# 期望返回 {"status":"healthy", ...} 含 api/engine/trade/stream 四服务
```

### 6.4 登录验证（关键：验证 users 表 + 认证链路）

```bash
curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123","tenant_id":"default"}'
# 期望返回 access_token；若 401/500 → users 表问题（见排查）
```

### 6.5 前端访问

```bash
curl -s -I http://localhost | head -1   # 期望 200 OK
```

### 6.6 登录后台 + 配置数据源

1. 浏览器访问 `http://服务器IP`，用 admin 登录
2. 后台「数据管理」配置 **QuantDB API Key**（见 \[\[quantdb-sdk]]）
3. 触发一次数据同步（见 \[\[quantmind-operations]] 第 3 节）

## 7. 更新部署

```bash
cd /opt/quantmind
# 一键更新脚本（同步代码 → 重建核心镜像 → 重启服务 → 导入数据库补丁 → 健康检查，不动数据库数据）
sudo bash deploy/update.sh
# 更新到指定分支或 tag
sudo bash deploy/update.sh --ref master
# 覆盖服务器上未提交的代码改动（谨慎，用 --force 而非 --force-sync）
sudo bash deploy/update.sh --force
# 纯代码改动，跳过镜像重建
sudo bash deploy/update.sh --no-build

# 手动更新
git pull origin master
docker compose build
docker compose up -d
```

### 7.1 update.sh 自动做了什么（客户升级说明）

| 步骤      | 行为                                                                                                                                                                               | 数据是否受影响                     |
| ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------- |
| 1 拉代码   | `git fetch origin $REF`；分支走 `checkout -B`,tag 走 `checkout --detach`；有未提交改动且未加 `--force` 时终止，`--force` 时 `reset --hard` + `clean`（排除 `data/models/db/logs/user_pools_local/.env`） | 否                           |
| 2 重建后端  | `docker compose build quantmind`（`--no-build` 跳过）                                                                                                                                | 否                           |
| 3 重启服务  | `up -d --no-deps --force-recreate quantmind [celery-worker celery-beat]` + `up -d --remove-orphans`                                                                              | 否                           |
| 4 数据库升级 | 扫描并执行 `data/upgrade_*.sql`（经 db 容器 `psql`）；**无去重/版本表/自动备份**，补丁需自身幂等                                                                                                              | 否（PG 数据在 `postgres-data` 卷） |
| 5 健康检查  | `curl http://127.0.0.1:8000/health`（30 次 × 2s），失败即终止                                                                                                                             | 否                           |

> ⚠️ 数据库补丁为**可重复执行的幂等 SQL**：每次 update 都会重跑一遍所有 `data/upgrade_*.sql`（无去重记录），需用 `IF NOT EXISTS` 等自防重。打新版本补丁时在 `data/` 新增 `upgrade_vX.Y.Z.sql` 即可，不用改脚本。

> ⚠️ update.sh 只重建后端 `quantmind` 镜像，**不** `build web`/data-gateway/dashboard。前端或可选服务代码有改动时，需走离线包成品镜像或手动 `docker compose build` 对应服务。

### 7.2 版本展示（升级后客户能看到当前版本）

- 后端 `/api/v1/system/version` 返回 `{"version": "...", "edition": "oss"}`；前端「用户中心 → 设置」页顶部「系统信息」卡片展示版本号。

- 版本号由 update.sh 在同步代码后经 `git describe --tags --always` 写入 `backend/shared/version.txt`（`.gitignore` 内，不入仓库）；本地未走 update.sh 时接口回落 `dev`。

### 7.3 升级后验证

```bash
# 容器健康 + 版本号
curl -s http://localhost:8000/api/v1/system/version
# 登录（数据库补丁执行无碍）
curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123","tenant_id":"default"}'
```

## 8. 云端 GPU 训练（AutoDL）

模型训练可跑在 **AutoDL 远程 GPU 节点**（本地 Docker 是 CPU 训练）。

### AutoDL 训练节点配置

```bash
# 列出训练节点（本地 Docker + AutoDL 远程 GPU）
curl -s -H "$AUTH" "$BASE/api/v1/admin/models/training-nodes"
# 测试节点连接（SSH + docker 可用性）
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/admin/models/training-nodes/test" \
  -d '{"node_id":"autodl-1"}'
# 新增/更新节点配置（SSH 凭据等）
curl -s -X POST -H "$AUTH" -H "$CT" "$BASE/api/v1/admin/models/training-nodes/config" \
  -d '{"node_id":"autodl-1","host":"<ip>","port":22,"user":"root","ssh_key":"<key>","description":"AutoDL 4卡A100"}'
# 节点实时状态（CPU/GPU/内存/训练容器）
curl -s -H "$AUTH" "$BASE/api/v1/admin/models/training-nodes/{node_id}/status"
# 节点详情 / 删除
curl -s -H "$AUTH" "$BASE/api/v1/admin/models/training-nodes/{node_id}/detail"
curl -s -X DELETE -H "$AUTH" "$BASE/api/v1/admin/models/training-nodes/{node_id}"
```

### AutoDL 远程训练镜像

```bash
# 在 AutoDL 节点从 git 直接构建（独立轻量镜像，仅训练依赖）
docker build --build-arg TORCH_DEVICE=gpu -f docker/autodl/Dockerfile -t quantmind-train:latest .
# 或一键远程构建脚本
bash scripts/setup/build-autodl-remote.sh
```

### 远程训练流程

1. **配节点**：`training-nodes/config` 存 AutoDL 节点 SSH 配置（`config/training_nodes.yaml`，含 SSH 凭据，gitignore 不入仓库）
2. **测连接**：`training-nodes/test` 验证 SSH + docker
3. **启动训练**：`run-training` 时选 `node_id=autodl-x`（GPU 训练）
4. **看状态**：`training-runs/{run_id}` 轮询；节点实时状态 `training-nodes/{node_id}/status`
5. **模型回传**：训练完模型 scp 回传注册到 `/models`

## 8b. AutoDL 实例内原生部署（无 Docker 环境）

> 实战记录（2026-08-16，RTX 5090 实例）。AutoDL 实例是容器（PID1=bash），**宿主机未开嵌套容器权限**，Docker 完全不可用。若需在实例内跑完整 QuantMind 平台（而非仅作训练节点），走原生部署。

### 环境探测（部署前必做）

```bash
# Docker 可行性判定：缺 SYS_ADMIN 能力位 + unshare 被禁 = 装不了 Docker（rootless 也不行）
grep CapEff /proc/self/status                      # a80425fb 缺 0x200000 (SYS_ADMIN)
unshare --user --map-root-user true 2>&1           # Operation not permitted → 无 userns
ps -p 1 -o comm=                                    # bash（非 systemd，开机自启只能挂 .bashrc）
```

### 原生部署步骤（全部装系统盘，保存镜像才完整）

````bash
# 1. 系统依赖（Ubuntu 22.04 源自带 PG14/Redis6，版本够用）
apt-get install -y postgresql redis-server nginx git build-essential cmake swig libgomp1

# 2. Python 3.10（对齐官方镜像；AutoDL 自带 miniconda 是 py3.12，必须另建环境）
conda create -n qm python=3.10 pip
source activate qm

# 3. torch GPU 版（走阿里云 PyPI，实测 4MB/s；官方 pytorch.org 在 AutoDL 被 403）
pip install torch==2.9.1 -i https://mirrors.aliyun.com/pypi/simple/

# 4. 其余依赖（与 Dockerfile.oss 同清单，requirements/*.txt + quantdb-sdk + patch_qlib）
pip install -r requirements.txt -r requirements/production.txt -r requirements/ai.txt
pip install "quantdb-sdk==0.3.3" && python docker/patch_qlib.py

# 5. PG 初始化（实例内无 systemd，用 pg_ctlcluster 拉起）
pg_ctlcluster 14 main start
su - postgres -c "psql -c \"CREATE ROLE quantmind LOGIN PASSWORD 'quantmind2026';\""
su - postgres -c "psql -c 'CREATE DATABASE quantmind OWNER quantmind;'"
PGPASSWORD=quantmind2026 psql -h 127.0.0.1 -U quantmind -d quantmind -f backend/shared/db_init.sql

# 6. Redis：redis-server --daemonize yes --port 6379

# 7. 环境变量：导出对齐 docker-compose.yml 的 env（DB_HOST=127.0.0.1，STORAGE_ROOT=数据目录）

# 8. 启动后端（setsid 彻底脱离 SSH 会话，否则 SSH 断开进程被杀）
cd /root/QuantMind && setsid nohup bash qm-start.sh > data/logs/backend.log 2>&1 < /dev/null &

# 9. 前端：本地 npm run build:react 构建 dist-react（~29M），scp 到 /usr/share/nginx/html/
#    nginx 反代 /api/→127.0.0.1:8000、/ws/→127.0.0.1:8003；$connection_upgrade 变量是
#    docker-nginx 专属，原生 nginx 直接写 Connection "upgrade"

# 10. 开机自启（无 systemd）：写 /etc/autodl.sh（AutoDL 官方开机钩子，PID1 boot.sh 会调用它）
#    内容：service cron start + 调用 /root/qm-autostart.sh（pg_ctlcluster + redis --daemonize + nginx + setsid 后端 + qwenpaw + huntly）
#    cron 看门狗兜底：apt install cron && service cron start && crontab -e 加 "* * * * * bash /root/qm-watchdog.sh"
#    ⚠️ .bashrc 自启无效——PID1 是 boot.sh，不经过交互式登录；实例重启后 cron 不自起，必须写进 autodl.sh

### QwenPaw 原生部署（无 Docker）
```bash
# 源码来源：本地 docker 容器 agentscope/qwenpaw:latest 里 /app 目录（含构建好的 console/dist）
# 打包：docker exec qwenpaw tar czf /tmp/qwenpaw-src.tgz src pyproject.toml setup.py  （~15MB，console 已在 src/qwenpaw/console/）
# 云端：
conda create -n qwenpaw python=3.11 pip -y          # 注意：conda 可能因网络重试失败但实际创建成功，用 ls envs/qwenpaw/bin 确认
/root/miniconda3/envs/qwenpaw/bin/pip install -e /root/QwenPaw -i https://mirrors.aliyun.com/pypi/simple/
/root/miniconda3/envs/qwenpaw/bin/pip install asyncpg redis psycopg2-binary -i https://mirrors.aliyun.com/pypi/simple/
# 环境变量对齐 docker-compose qwenpaw 段：PYTHONPATH=/app + /app 下符号链接到 QuantMind（backend/config/scripts/working/models/logs/db）
#   已补 /etc/hosts: db→127.0.0.1、redis→127.0.0.1、qwenpaw→127.0.0.1、copaw→127.0.0.1
qwenpaw init --defaults --accept-security            # 生成 /app/working/config.json
qwenpaw app --host 0.0.0.0 --port 8088               # 启动（用 /root/qwenpaw-start.sh 带启动锁）
# 后端连 QwenPaw：.env.sh 加 QWENPAW_BASE_URL=http://127.0.0.1:8088 + COPAW_BASE_URL，重启后端
# 前端访问：/api/v1/qwenpaw-ui/ 代理（无需 8088 直接暴露）
````

### Huntly 原生部署（无 Docker）

````bash
# 源码来源：本地 docker cp quantmind-huntly:/app/server.jar（~121MB，scp 约 20 分钟）
apt-get install -y default-jre-headless               # JRE 11 即可
java -Xms128m -Xmx1024m -Duser.timezone=GMT+08 -jar server.jar \
  --spring.profiles.active=default --server.port=8090 \
  --huntly.dataDir=/root/huntly/data/ --huntly.luceneDir=/root/huntly/data/lucene
# ⚠️ 首次启动自动建用户 changeme（HUNTLY_DEFAULT_USERNAME/PASSWORD 只在首次生效，之后改环境变量无效）
#    要改账号：sqlite3 db.sqlite "UPDATE users SET username='admin', password='<bcrypt>' WHERE username='changeme'"
#    密码是 bcrypt(10)，用 python bcrypt.hashpw(b"admin123", bcrypt.gensalt(10))
# 后端连 Huntly：.env.sh 里 HUNTLY_USERNAME/HUNTLY_PASSWORD 改对后重启后端，news/health 应返回 up
# ⚠️ 8090 不在 AutoDL 公网映射（只有 6006/6008 映射），前端「后台」链接打不开：
#    方案 A（已落地）：后端 /api/v1/news/huntly-ui/ 做 SPA 子路径代理（见下方「Huntly UI 代理」）——
#    HTML 路径重写 + JS 拦截脚本重写 fetch/XHR/WebSocket/EventSource 的 /api/ → 代理路径
#    前端资讯页「后台」按钮指向 /api/v1/news/huntly-ui/（SERVICE_ENDPOINTS.USER_SERVICE 拼接），无需 8090 公网暴露
#    方案 B（备用）：nginx 单独 server 块 listen 6008 反代 127.0.0.1:8090（6008 映射到公网 443，前端 6006 映射 8443）

### Huntly UI 代理（SPA 子路径代理模式，news.py 已实现）
```bash
# 访问入口：https://<实例ID>.westd.seetacloud.com:8443/api/v1/news/huntly-ui/
# 登录：admin / admin123（Huntly 自己的账号体系，与 QuantMind 后端账号无关）
````

SPA 子路径代理必须做对四件事，缺一即部分功能失灵（实战踩坑记录）：

1. **HTML 路径重写**：`src="/static/` → `src="/api/v1/news/huntly-ui/static/`、favicon/manifest 同理
2. **JS 拦截脚本注入** **`<head>`**：重写 `fetch`/`XMLHttpRequest.open`/`WebSocket` 的 `/api/` 前缀 → 代理路径
   （EventSource 构造也传 `/api/...` 字符串，走 fetch 重写不够，Huntly 用的是 `new EventSource("/api/...")`，需要把 EventSource 也包一层或确认其 URL 已被拦截；当前实现已覆盖）
3. **API 路由声明顺序**：FastAPI 按声明顺序匹配，`@router.api_route("/huntly-ui/api/{path:path}")` 必须声明在
   `@router.get("/huntly-ui/{path:path}")`（静态）**之前**，否则 GET 会被静态路由吞掉 → 不带鉴权转发 → 405/401
4. **Set-Cookie 透传**：Huntly 是 HttpOnly cookie 会话（`auth_token=...; Path=/; HttpOnly`），前端 JS 里根本不存 token。
   代理响应必须透传 `set-cookie` 头，浏览器才会把 cookie 存在 QuantMind origin 下；转发时后端 `_ensure_session()`
   的全局 JWT 兜底（带 Authorization + Cookie 双头）保证登录前/后 API 都通

```
```

### AutoDL 原生部署踩坑清单

| 坑                              | 现象                                                                                                                                                                                 | 解法                                                                                                                                                           | <br />                             | <br />                                                                                                                                                                                                                                                |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Docker 装不上**                 | unshare/mount 全被拒                                                                                                                                                                  | 放弃 docker，原生部署（本表）                                                                                                                                           | <br />                             | <br />                                                                                                                                                                                                                                                |
| **/.dockerenv 触发容器重定向**        | AutoDL 实例**本身就是容器**（存在 /.dockerenv），`config.py` 检测到后把 REDIS\_HOST 强制改成 `quantmind-redis` → 原生部署无此 DNS，登录卡 \~24s DNS 超时                                                             | 把代码里所有 quantmind-\* 容器名全部写入 /etc/hosts → 127.0.0.1（\`grep -rhoE 'quantmind-\[a-z0-9\_-]+' backend --include='\*.py'                                          | sort -u\` 枚举，\~22 个）；改完重启后端登录 0.3s | <br />                                                                                                                                                                                                                                                |
| **外网代码源全废**                    | GitHub 0-25KB/s、Gitee 613B/s、ghproxy 全超时、ACR 426B/s                                                                                                                                | 代码走**本地打包 scp**（瘦身后 \~16MB）；依赖走**阿里云 PyPI**（4MB/s 唯一快源）                                                                                                      | <br />                             | <br />                                                                                                                                                                                                                                                |
| **SSH 直传限速**                   | \~100KB/s（30MB 传 5 分钟）                                                                                                                                                             | ①砍体积：exclude 掉 scenarios/fonts/torch\_wheels/rd-agent/bridge-windows 等大目录 ②并行传多个小包                                                                           | <br />                             | <br />                                                                                                                                                                                                                                                |
| **tar exclude 误伤**             | `--exclude='models'` 把 `backend/services/*/models` 全部剔除 → 四服务 ModuleNotFoundError 崩 5 次                                                                                            | 排除用精确路径；传完必须 `find backend -type d -name models` 对比本地远端                                                                                                      | <br />                             | <br />                                                                                                                                                                                                                                                |
| **pkill 断 SSH**                | pkill 模式匹配到 SSH 会话自身命令 → 连接断开（exit 255/144）                                                                                                                                        | 用 `pkill -f 'main_oss[.]py'` 正则字符类防自匹配；启动用 `setsid nohup ... < /dev/null`                                                                                    | <br />                             | <br />                                                                                                                                                                                                                                                |
| **nohup 目录未建**                 | 日志目录不存在导致启动静默失败                                                                                                                                                                    | 启动脚本里先 mkdir -p 所有数据/日志目录                                                                                                                                    | <br />                             | <br />                                                                                                                                                                                                                                                |
| **conda py312 不兼容**            | qlib 等依赖锁 py3.10                                                                                                                                                                   | 必须 `conda create -n qm python=3.10`                                                                                                                          | <br />                             | <br />                                                                                                                                                                                                                                                |
| **python 脚本生搬硬套**              | 用 `python3` 而非 conda 环境 python                                                                                                                                                     | 所有启动/验证用 `/root/miniconda3/envs/qm/bin/python`                                                                                                               | <br />                             | <br />                                                                                                                                                                                                                                                |
| **无 systemd**                  | systemctl 是摆设                                                                                                                                                                      | PG 用 pg\_ctlcluster、Redis 用 --daemonize、自启挂 /etc/autodl.sh（非 .bashrc）；`apt install cron` 后还要 `service cron start` 并写进 /etc/autodl.sh（实例重启后 cron 不会自起，看门狗就废了） | <br />                             | <br />                                                                                                                                                                                                                                                |
| **看门狗 pgrep 失灵**               | main\_oss 的 uvicorn worker 经 multiprocessing.spawn 后 cmdline 被重写为 `spawn_main`（不含 main\_oss 字样）且 PPID=1，`pgrep -f main_oss` 永远匹配不到 → 看门狗每分钟误判重复拉起                                  | 看门狗按**端口监听**判断（\`ss -tln                                                                                                                                     | grep ":8000 "\`），不能用 pgrep -f      | <br />                                                                                                                                                                                                                                                |
| **两代进程混居**                     | 杀进程时 pkill 模式没匹配到主进程（如 `pkill -f main_oss[.]py` 匹配不到、`envs/qm` 匹配不到主进程）→ 旧 worker 占着端口，新启动的主进程端口绑定失败，日志刷 "crashed too many times (5/5)" 死循环，health 却显示 healthy（响应的是没人管的旧孤儿 worker） | ①清场用 \`ps -eo pid,cmd                                                                                                                                        | grep -E "main\_oss                 | envs/qm"  ` 枚举出 PID 逐个 kill -9（避免 pkill 自匹配）——**必须把 spawn_main 孤儿也杀掉**（它们的 cmdline 不含 main_oss，`ps  ` 里只有 spawn_main/resource_tracker 字样）②qm-start.sh 加**启动锁**（/tmp/qm-start.lock + trap EXIT 释放）防重复启动 ③验证：`ps`里`backend/main\_oss\` 恰好 1 个进程且四端口由它监听 |
| **Huntly UI 代理 401/405**       | GET /huntly-ui/api/\* 经代理全 401/500——FastAPI 路由声明顺序问题：静态 `/{path:path}` 先于 api\_route 声明，GET 被静态路由吞掉不带鉴权转发                                                                          | api\_route 必须声明在静态路由之前（见「Huntly UI 代理」）；Set-Cookie 必须透传（Huntly 是 HttpOnly cookie 会话）                                                                         | <br />                             | <br />                                                                                                                                                                                                                                                |
| **qm-start.sh 路径写错**           | cd /root/QuantMind 后 `bash qm-start.sh` 报 No such file——脚本在 /root/ 不在仓库里                                                                                                           | 用绝对路径 `bash /root/qm-start.sh`                                                                                                                               | <br />                             | <br />                                                                                                                                                                                                                                                |
| **nginx $connection\_upgrade** | docker-nginx 专属变量，原生 nginx 配置报错/ws 不通                                                                                                                                              | websocket 反代直接写 `Connection "upgrade"`                                                                                                                       | <br />                             | <br />                                                                                                                                                                                                                                                |
| **AutoDL 端口映射**                | 只有 6006/6008 映射到公网（https\://<实例ID>.westd.seetacloud.com:8443/443），8000-8003 不映射                                                                                                    | nginx 额外 listen 6006（80 默认 server 的同一 server 块加 listen），对外访问走 8443                                                                                           | <br />                             | <br />                                                                                                                                                                                                                                                |
| **修改需重启才生效**                   | 改 /etc/hosts、.env 后旧进程还在                                                                                                                                                           | pkill（防自匹配）→ 重跑 qm-start.sh → 验证四端口 health + 登录                                                                                                              | <br />                             | <br />                                                                                                                                                                                                                                                |

### 部署后验证（AutoDL 原生）

```bash
for p in 8000 8001 8002 8003; do curl -s http://127.0.0.1:$p/health; done   # 四服务 healthy
time curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123","tenant_id":"default"}'      # 返回 access_token 且 <1s
#   若 >20s 必是 /.dockerenv 陷阱（见踩坑清单第 2 条）
/root/miniconda3/envs/qm/bin/python -c "import torch; print(torch.cuda.get_device_name(0))"

# 外网访问（AutoDL 控制台「自定义服务」把 6006 映射成公网 8443 后）：
curl -skI https://<实例ID>.westd.seetacloud.com:8443/ | head -1               # 前端 200
curl -sk -X POST https://<实例ID>.westd.seetacloud.com:8443/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123","tenant_id":"default"}'      # 公网登录链路 200
```

## 9. 问题排查（诊断树，按顺序走）

### 9.1 先看这 3 条命令的输出（快速定位）

```bash
# 1. 容器状态（谁没起来）
docker compose -f /opt/quantmind/docker-compose.yml ps

# 2. 后端日志（报什么错）
docker compose -f /opt/quantmind/docker-compose.yml logs --tail=100 quantmind

# 3. 数据库连接
docker exec quantmind-db pg_isready -U quantmind
```

### 9.2 诊断树

```
部署后不能访问？
├─ curl localhost:8000/api/v1/health 失败
│   ├─ 容器没起来 → docker compose ps 看状态 → docker compose logs quantmind 看报错
│   ├─ 端口被占 → netstat -tlnp | grep 8000 → 释放冲突端口
│   └─ 数据库连不上 → docker exec quantmind-db pg_isready → 重启 db 容器
├─ health OK 但登录失败（401/500）
│   ├─ users 表不存在 → docker exec quantmind-db psql -U quantmind -d quantmind -c "\dt users"
│   │     → 无表则手动执行 db_init.sql（见第 5 节）
│   └─ SECRET_KEY 不一致 → 检查 .env 的 JWT_SECRET_KEY，重启后端
├─ 登录成功但前端打不开
│   ├─ curl -s -I http://localhost 失败 → nginx -t → systemctl restart nginx
│   ├─ PM2 没起 → pm2 status → pm2 restart quantmind-web
│   └─ 前端构建问题 → cd /opt/quantmind/electron && npm install && npm run dashboard:build
└─ 都正常但页面报"数据缺失"
    ├─ 未配 QuantDB API Key → 后台「数据管理」填 Key（见 [[quantdb-sdk]]）
    └─ 未同步数据 → [[quantmind-operations]] 第 3 节触发同步
```

### 9.3 常见问题速查表

| 现象                      | 原因               | 处理                                                       |
| ----------------------- | ---------------- | -------------------------------------------------------- |
| **用户表不存在 / 登录失败**       | db\_init.sql 未执行 | 手动执行 db\_init.sql（见第 5 节）；确认 `\dt users`                 |
| **Docker Compose 版本过低** | 需 v2.19+         | `docker compose version`，装 docker-compose-plugin         |
| **镜像拉取慢/失败**            | 网络源              | 脚本自动选 Docker/PyPI/APT 镜像源，可手动 `--build-arg` 指定           |
| **torch 安装失败**          | GPU/CPU 兼容       | Dockerfile 支持 `TORCH_DEVICE=cpu/gpu/skip`（skip 适合纯行情/交易） |
| **容器起不来**               | 端口冲突 / 配置        | `docker compose logs quantmind` 看日志                      |
| **数据库连接失败**             | PG 未就绪           | `docker exec quantmind-db pg_isready -U quantmind`       |
| **前端 502**              | Nginx/PM2        | `nginx -t` + `pm2 status` + `pm2 restart quantmind-web`  |
| **quantdb-sdk 安装失败**    | 版本兼容             | Dockerfile 用 `quantdb-sdk==0.3.3`，换源重装                   |
| **北向/南向无数据**            | 未同步              | 跑 `quantdb_north_sync` / `quanthk_south_sync`            |
| **GPU 训练不生效**           | 未配 AutoDL 节点     | `training-nodes/config` 配置后选 node\_id                    |
| **AutoDL 节点连不上**        | SSH 配置错          | `training-nodes/test` 诊断 SSH/docker                      |

### 9.4 AI 助手部署常见坑（给编程 AI 的提示）

| AI 常犯错误             | 正确做法                                       | <br />                     |
| ------------------- | ------------------------------------------ | -------------------------- |
| 跳过交互式确认             | `quick-deploy.sh` 是交互式，用 \`echo y          | sudo bash ...`或确认到`--yes\` |
| 忽略系统版本              | 必须先 `check_system`（仅 Ubuntu 22.04+）        | <br />                     |
| 不检查 users 表         | 部署后必查 `\dt users`，这是登录失败主因                 | <br />                     |
| 直接 `docker-compose` | 新版用 `docker compose`（带空格）                  | <br />                     |
| 忘配 QuantDB Key      | 部署完成≠能用，还需在后台填 API Key + 同步数据              | <br />                     |
| 端口冲突硬上              | 先 `netstat -tlnp` 看占用，改 compose 端口         | <br />                     |
| 忘重启容器               | 改 `.env`/代码后要 `docker compose restart` 才生效 | <br />                     |
| 直接用 gitee master    | 生产固定 `v1.9.0-beta` tag，可复现                 | <br />                     |

## 数据目录（持久化）

```
/opt/quantmind/data/
├── postgres/       # 数据库数据
├── redis/          # Redis 数据
├── quantdb/        # QuantDB A股 parquet
├── quantus/        # 美股 parquet
├── quanthk/        # 港股 parquet
├── quantbc/        # 区块链 parquet
├── quantfutures/   # 期货 parquet
├── logs/           # 日志
└── models/         # 模型文件
```

## 相关技能

- **\[\[quantmind-operations]]** — 部署后数据同步、模型训练、推理

- **\[\[quantdb-sdk]]** — QuantDB 数据源配置（API Key）

- **\[\[simulation-trading]]** — 部署后模拟盘验证

