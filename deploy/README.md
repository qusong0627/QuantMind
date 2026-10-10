# QuantMind 部署指南

## 选择部署方式

| 方式 | 适用场景 | 入口 |
| --- | --- | --- |
| 完整部署 | 从 CDN 下载完整业务数据、模型与 Qlib 数据包，一键迁移；开箱即用 | `full-deploy.sh` |
| 在线源码部署 | 新服务器可稳定访问代码和镜像仓库，部署后另行准备数据 | `deploy.sh` |
| 一键更新 | 已部署服务器更新代码和核心服务 | `update.sh` |
| AutoDL GPU 训练 | 主节点已就绪，把模型训练卸到 AutoDL 显卡实例（免 Docker） | `autodl/README.md` |

所有脚本支持 Ubuntu 22.04 / 24.04，默认项目目录为 `/opt/quantmind`。完整说明（含 AutoDL 节点）见 **[docs/部署指南.md](../docs/部署指南.md)**。

## 完整部署

完整部署包 CDN 目录默认是 `https://cdn.quantmind.cloud/quantmind-offline`，应包含：

```text
SHA256SUMS
images.tar.zst
data-system.tar.zst
postgres-all.sql.zst
quantmind_qwenpaw-*.tar.zst
README.txt
```

```bash
curl -fsSL https://gitee.com/qusong0627/QuantMind/raw/master/deploy/full-deploy.sh | sudo bash
```

可覆盖完整部署包地址、代码分支或 Docker 加速地址：

```bash
sudo QUANTMIND_OFFLINE_BASE_URL='https://example.com/quantmind-offline' \
  QUANTMIND_REF='master' \
  QUANTMIND_DOCKER_MIRROR='https://你的镜像加速域名' \
  bash deploy/full-deploy.sh
```

脚本默认保留已有 Qlib、业务目录、数据库和 QwenPaw 卷。确认需要覆盖时再传入：

```bash
QUANTMIND_REPLACE_QLIB=true \
QUANTMIND_REPLACE_BUSINESS_DATA=true \
QUANTMIND_REPLACE_DATABASE=true \
QUANTMIND_REPLACE_QWENPAW_DATA=true
```

## 离线包制作与依赖指纹

`full-deploy.sh` 用「依赖指纹」在速度与新鲜度之间自动决策：镜像构建时把
requirements 指纹写入 Label `qm.req.sha`，部署时与当前代码算出的指纹比对——
一致直接复用成品镜像（秒级）；不一致（如依赖新增了包）自动重建对齐。

**制作镜像时必须打指纹戳**（否则部署侧视为无指纹、每次触发重建）：

```bash
QM_REQ_SHA=$(bash deploy/req-fingerprint.sh) docker compose build quantmind
docker save quantmind-oss:latest <其余镜像...> | zstd -T0 -o images.tar.zst
```

**部署真相戳（T7-3）**：`deploy/{update,full-deploy,deploy}.sh` 构建时会自动注入
`QM_GIT_COMMIT/QM_GIT_BRANCH/QM_GIT_DIRTY`，写入镜像 Label（`docker inspect` 可核对
`qm.git.*`、`qm.torch.device`）与 `/app/deploy_stamp.json`。手工构建建议同样传入
（不传=unknown；容器启动日志与 `/api/v1/system/deploy-truth` 会如实报告身份，
不伪造成某版）：

```bash
QM_REQ_SHA=$(bash deploy/req-fingerprint.sh) \
QM_GIT_COMMIT=$(git rev-parse HEAD) \
QM_GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD) \
QM_GIT_DIRTY=$(test -n "$(git status --porcelain)" && echo true || echo false) \
docker compose build quantmind
```

指纹只覆盖 `requirements.txt`、`requirements/{production,ai}.txt`、
`docker/Dockerfile.oss` 与 `TORCH_DEVICE` 取值。**出片纪律（P2-6 起）**：
images.tar.zst 内的 `quantmind-oss:latest` 必须与随包代码**同一 commit** 构建
（上面示例已含 QM_GIT_COMMIT），否则部署侧身份闸门判定不一致会强制重建——
纯离线机将直接失败停工（fail-closed，见下节）。requirements/Dockerfile 变更后
必须重打 images.tar.zst；联网部署机会自动重建补齐（保持服务可用）。

## 不可变发布（P2-6）

生产发布路径 = `docker-compose.yml` + `docker-compose.prod.yml` 覆盖层：三个应用
容器（quantmind / celery-worker / celery-beat）**不再挂载任何代码或出厂文件**，
容器里跑的代码 = 镜像里烘焙的代码，与 `qm.git.commit` Label 和
`/app/deploy_stamp.json` 可逐位核对——结构上消灭「热修 / 并行改动 / 挂载 inode
陈旧」导致的运行态与镜像身份脱节。本地开发不受影响（裸 `docker-compose.yml`
仍是全量挂载热更新）。

发布流程 = **构建（注戳）→ 晋级（:latest）→ 重启 → 断言**：

- `update.sh`：镜像身份（`qm.git.commit`）== 检出 HEAD 才允许复用，不等即重建；
  重启后断言三个容器零代码挂载且运行态戳==检出，**不过则自动回滚上一镜像并重启**，
  不执行任何 SQL；`--no-build` 仅在镜像身份==检出时允许。
- `full-deploy.sh` / `deploy.sh`：同一断言；构建前额外比对镜像身份与检出
  HEAD（不一致强制重建），失败即停（全新安装语境无上一镜像可回滚）。

验证探针：

```bash
docker inspect --format '{{ index .Config.Labels "qm.git.commit" }}' quantmind-oss:latest
docker inspect --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' quantmind   # 只应有数据类路径
docker exec quantmind cat /app/deploy_stamp.json
```

要求 docker compose ≥ 2.24.4（卷替换标签 `!override`）；过旧时脚本会明确报错并给出
升级指引。应急回退挂载模式（仅排障用）：`update.sh --mounts` 或
`QUANTMIND_BIND_MOUNTS=true`。回归测试：`backend/tests/test_release_immutable.py`
（覆盖层零代码挂载、剥离↔烘焙映射、挂载过滤器行为）。

## 在线源码部署

```bash
sudo bash deploy/deploy.sh
```

常用参数：

```bash
sudo bash deploy/deploy.sh --ref NEXT
sudo bash deploy/deploy.sh --force
```

在线脚本会安装运行时、配置 Docker 镜像加速、同步代码、首次生成 `.env`、构建核心镜像并启动 Compose 服务。

## 一键更新

```bash
cd /opt/quantmind
sudo bash deploy/update.sh
```

```bash
sudo bash deploy/update.sh --ref NEXT
sudo bash deploy/update.sh --force
sudo bash deploy/update.sh --no-build
sudo bash deploy/update.sh --mounts   # 应急：退回 bind-mount 模式（仅排障）
```

更新脚本只同步代码和核心容器，不会默认删除 PostgreSQL、Redis、`data/`、`models/` 或 `db/qlib_data/`，并会自动导入 `data/upgrade_*.sql` 数据库升级补丁（补丁需保持幂等，可重复执行）。

## 验证与排障

```bash
cd /opt/quantmind
docker compose ps
docker compose logs --tail=200 quantmind
curl http://127.0.0.1:8000/health
```

| 服务 | 默认端口 |
| --- | --- |
| Web | 3000 |
| API / Engine / Trade / Stream | 8000 / 8001 / 8002 / 8003 |
| Data Gateway | 8004 |
| Huntly / RSSHub / QwenPaw | 8090 / 1200 / 8088 |

## AutoDL 远程 GPU 训练

不要把整套平台装进 AutoDL。主节点继续跑 Docker，AutoDL 只做 **`native_python` 训练 Worker**（实例一般不能嵌套 Docker）。

1. AutoDL 上执行 `deploy/autodl/setup-autodl-native.sh`，或一条命令下载执行 `deploy/autodl/quick-setup.sh`（详见 [`autodl/README.md`](autodl/README.md)）。
2. 主节点写 `config/training_nodes.yaml`（gitignore），`exec_mode: native_python`，数据目录 `/root/autodl-fs/quantdb`。
3. `.env` 设置 `TRAINING_MASTER_HOST=<协调机公网IP>`，重启 `quantmind`。
4. 桌面客户端模型训练页选择该节点。

端到端步骤、端口变更与排障见 **[docs/部署指南.md 第十一节](../docs/部署指南.md)**。
