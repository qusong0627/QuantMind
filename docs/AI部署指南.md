# QuantMind AI 部署指南（next 分支）

> 目标读者：**AI 代理 / 自动化脚本**。本文所有命令都可直接执行，不需要人工判断。
> 人类用户请看 [部署指南.md](部署指南.md)。
>
> - 本文对应分支：**`next`**（Gitee 默认分支是 `master`，安装稳定版把文中 `next` 换成 `master`）
> - 部署脚本：`deploy/full-deploy.sh`（完整包）或 `deploy/deploy.sh`（在线源码）
> - 部署目录：默认 `/opt/quantmind`，可用 `QUANTMIND_PROJECT_DIR` 覆盖
> - 全部脚本**非交互**、幂等、必须 root 执行

---

## 0. 执行契约（先读，违反会装错）

**必须满足**

- 操作系统：Ubuntu **22.04 / 24.04**（x86_64）；其他系统脚本会直接报错退出
- 权限：`root` 或 `sudo`
- 资源：≥ 4 核 / 8 GB 内存 / `/opt` 可用 ≥ 100 GB
- 网络：能访问 `gitee.com`、Docker Hub（或镜像加速）、CDN（完整包约 5 GB）

**两种模式二选一，不要都跑**

| 模式 | 脚本 | 是否含业务数据 | 适用 |
| --- | --- | --- | --- |
| 完整包（推荐） | `full-deploy.sh` | ✅ 含数据库备份 / Qlib / 预训练模型 | 新服务器开箱即用 |
| 在线源码 | `deploy.sh` | ❌ 需另行准备数据 | 有外网、想自己准备数据 |

**禁止事项（AI 不要做）**

- ❌ 不要设置 `ENABLE_REAL_TRADING=true` / `VITE_ENABLE_REAL_TRADING=true`（实盘默认关闭）
- ❌ 不要执行 `docker compose down -v`（会删除数据库卷）
- ❌ 不要删除 `/opt/quantmind/.env`、`data/`、`models/`
- ❌ 端口冲突时不要杀未知进程；停下并报告
- ❌ 不要跳过健康检查就宣布成功

**成功判据（三条全过才算完成）**

1. `curl -fsS http://127.0.0.1:8000/health` 返回 `{"status":"healthy","service":"quantmind-api"}`
2. `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:3080/` 返回 `200`
3. `docker ps` 中 `quantmind`、`quantmind-db`、`quantmind-redis`、`quantmind-celery`、`quantmind-celery-beat`、`quantmind-web` 均为 `Up`

---

## 1. 环境预检（逐条执行，任一不满足就停止并报告）

```bash
# 1.1 系统版本：期望 ID=ubuntu，VERSION_ID=22.04 或 24.04
cat /etc/os-release | grep -E '^(ID|VERSION_ID)='

# 1.2 架构：期望 x86_64
uname -m

# 1.3 资源：期望 CPU ≥ 4，内存 ≥ 8 GB（free -g 显示 7 属正常），/opt 可用 ≥ 100 GB
nproc
free -g | awk '/Mem:/{print "mem_gb=" $2}'
df -h /opt | tail -1

# 1.4 端口：期望输出 "ports free"
ss -lntp | grep -E ':(3080|5432|6379|8000|8001|8002|8003|8088)\b' || echo 'ports free'
```

> 若 5432/6379 已被其他 PostgreSQL/Redis 占用，停止并报告；不要复用陌生实例。

---

## 2. 部署

### 2.1 完整包（推荐，含数据）

```bash
# 下载脚本到本地（先落盘再执行，便于检查与重试）
curl -fsSL https://gitee.com/qusong0627/QuantMind/raw/next/deploy/full-deploy.sh \
  -o /tmp/quantmind-full-deploy.sh

# 语法检查：期望输出 syntax OK
bash -n /tmp/quantmind-full-deploy.sh && echo 'syntax OK'

# 后台执行（约 15–50 分钟，取决于下载速度；日志写 /tmp，普通用户可读）
sudo env QUANTMIND_REF=next QUANTMIND_REPO_URL=https://gitee.com/qusong0627/QuantMind.git \
  nohup bash /tmp/quantmind-full-deploy.sh > /tmp/quantmind-deploy.log 2>&1 &
echo "deploy_pid=$!"
```

### 2.2 在线源码（不含数据）

```bash
curl -fsSL https://gitee.com/qusong0627/QuantMind/raw/next/deploy/deploy.sh \
  -o /tmp/quantmind-deploy.sh

bash -n /tmp/quantmind-deploy.sh && echo 'syntax OK'

sudo env QUANTMIND_REF=next QUANTMIND_REPO_URL=https://gitee.com/qusong0627/QuantMind.git \
  nohup bash /tmp/quantmind-deploy.sh > /tmp/quantmind-deploy.log 2>&1 &
echo "deploy_pid=$!"
```

### 2.3 轮询进度（每 30–60 秒一次）

```bash
tail -n 40 /tmp/quantmind-deploy.log
pgrep -f 'quantmind-(full-)?deploy.sh' >/dev/null && echo 'still running' || echo 'process finished'
```

**结束标记**

- 完整包成功：日志出现 `🎉 QuantMind 完整部署成功！`
- 在线源码成功：日志出现 `[quantmind-deploy] 部署完成：/opt/quantmind`
- 失败：日志出现 `错误:` / `die` / 进程退出但未见成功标记 → 进入第 6 节

---

## 3. 验收（必须执行）

```bash
# 3.1 API 健康：期望 {"status":"healthy","service":"quantmind-api"}
curl -fsS http://127.0.0.1:8000/health

# 3.2 Web 控制台：期望 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3080/

# 3.3 容器状态：期望关键容器全部 Up
docker ps --format '{{.Names}}\t{{.Status}}' \
  | grep -E 'quantmind(-db|-redis|-celery|-celery-beat|-web|-dsh)?\b'

# 3.4 版本核对（完整包为 detached HEAD，用 commit 核对）
git -C /opt/quantmind rev-parse HEAD
docker exec quantmind cat /app/deploy_stamp.json 2>/dev/null || true
```

> 完整包/在线源码都以 `QUANTMIND_REF=next` 安装时，`HEAD` 应属于 next 分支最新提交。
> `deploy_stamp.json` 中的 `git.commit` 应与 `HEAD` 一致。

**默认登录**：`http://<服务器IP>:3080`，账号 `admin / admin123`。
必须提示人类用户：**首次登录后立即修改密码**。

---

## 4. 数据初始化

### 4.1 完整包

已含基础业务数据，直接检查：

```bash
du -sh /opt/quantmind/data/quantdb 2>/dev/null
ls /opt/quantmind/data/quantdb | head
```

> 目录不存在或为空 → 按 4.2 处理。

### 4.2 在线源码（或完整包补全）

二选一：

- **ModelScope 离线全量包（推荐，约 56 GB / 7.4 万文件）**
  浏览器进入 `http://<服务器IP>:3080` →【管理后台】→【数据管理】→【初始化数据】，
  数据集固定为 `qusong0627/LightGBM_Alpha300`，等待任务完成。
- **QuantDB 在线增量同步**
  先在【个人中心】→【数据平台】填入 QuantDB API Key，然后：

  ```bash
  docker exec quantmind python backend/scripts/quantdb_daily_sync.py
  ```

> 数据初始化是长任务（可能数小时），不要用前端超时判断失败；以【数据管理】任务状态为准。

---

## 5. 可选：Pine 策略库 / 批量回测（宿主 worker）

不在容器内运行，需要宿主机 Python ≥ 3.11 + `bwrap`。**失败不阻塞主系统**。

```bash
cd /opt/quantmind
bash scripts/setup_pine_lab.sh      # 建 pine_lab/.venv（pynecore）
bash scripts/setup_host_workers.sh  # 幂等安装三条 cron
crontab -l | grep -E 'pine_|lab_batch'   # 期望看到 3 条任务
```

若 `bwrap` 不存在：Ubuntu 执行 `sudo apt-get install -y bubblewrap` 后重试；
仍失败则跳过并在最终报告中标注「Pine 运行时未安装」。

---

## 6. 失败处理

| 日志 / 现象 | 含义 | AI 动作 |
| --- | --- | --- |
| `错误: 请使用 sudo 执行` | 非 root | 用 `sudo` 重跑同一命令 |
| `错误: 仅支持 Ubuntu` | 系统不支持 | 停止，报告需要 Ubuntu 22.04/24.04 |
| `Docker Compose 不可用` | compose 未装或过旧 | `sudo apt-get install -y docker-compose-plugin`；要求 ≥ 2.24.4 |
| `检测到未提交代码改动` | 部署目录有本地改动 | 不要擅自覆盖；报告并等待人类确认，必要时加 `--force` |
| `服务未在 60 秒内通过健康检查` | 服务未起 | `docker logs --tail 200 quantmind`；`docker ps -a --filter name=quantmind`；修复后重跑部署（幂等） |
| `前端 quantmind-web 未就绪` | Web 容器失败 | `docker logs quantmind-web --tail=100`；`docker ps -a --filter name=quantmind-web` |
| `发布断言失败` | 镜像身份 ≠ 检出 commit | 不要绕过；把日志最后 100 行报告给人类 |
| `git clone` / `fetch` 失败 | 仓库地址不可达 | 追加 `QUANTMIND_REPO_URL=https://gitee.com/qusong0627/QuantMind.git` 重跑 |
| 下载慢 / 超时 | 国内网络 | 设置 `QUANTMIND_DOCKER_MIRROR`（Docker）、`QUANTMIND_PIP_MIRROR`（pip）、`QUANTMIND_OFFLINE_BASE_URL`（完整包 CDN） |
| `/opt` 空间不足 | 磁盘 < 100 GB | 停止，不要继续 |
| 端口被占用 | 已有服务 | 停止并报告占用进程，不要杀进程 |

通用重试：部署脚本幂等，修复原因后**重跑同一条命令**即可；不要手工删库、删卷。

---

## 7. 更新

```bash
cd /opt/quantmind
sudo bash deploy/update.sh            # 跟随当前 checkout 分支（next 安装后仍更新 next）
sudo bash deploy/update.sh --ref next # 显式指定 next
```

更新会：同步代码 → 重建必要镜像 → 重启服务 → 导入 `data/upgrade_*.sql` → 健康检查。
不会删除 PostgreSQL、Redis、`data/`、`models/`。

---

## 8. 最终报告模板（部署完成后原样输出）

```text
部署模式：full-deploy / deploy
版本：next @ <git rev-parse HEAD 前 12 位>
部署目录：/opt/quantmind
耗时：<分钟>
健康检查：api=<healthy/degraded>，web=<200/其他>
容器：<列出关键容器状态>
数据：<quantdb 大小 / 是否已初始化>
Pine worker：<已安装 / 未安装（原因）>
警告：<逐条列出>
后续动作：登录 http://<IP>:3080 修改 admin 密码；按需初始化数据
日志：/tmp/quantmind-deploy.log
```

---

## 9. 给 AI 的最小执行顺序（TL;DR）

1. 预检（第 1 节）→ 任一不满足：停止并报告
2. 选模式 → 下载脚本 → `bash -n` 检查 → `sudo env QUANTMIND_REF=next [QUANTMIND_REPO_URL=...] nohup bash ... > /tmp/quantmind-deploy.log 2>&1 &`
3. 轮询日志，直到出现成功标记或明确报错
4. 验收（第 3 节）→ 三条全过
5. 数据检查/初始化（第 4 节）
6. 可选 Pine worker（第 5 节）
7. 按第 8 节模板汇报
