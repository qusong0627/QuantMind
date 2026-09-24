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

指纹只覆盖 `requirements.txt`、`requirements/{production,ai}.txt`、
`docker/Dockerfile.oss` 与 `TORCH_DEVICE` 取值；**业务代码走 bind mount，
纯代码更新不需要重新制作镜像包**。requirements/Dockerfile 变更后才需重打
images.tar.zst；来不及重打包时，联网部署机会自动重建补齐（保持服务可用）。

`TORCH_DEVICE` **默认 `cpu`**（初始部署强制 CPU 版：镜像小、构建快、无 GPU 依赖），
实际形态写入镜像 `/etc/quantmind/torch-device`。需要 GPU（CUDA 版 torch + 本地训练
镜像）时执行 `sudo bash deploy/enable-gpu.sh`（`--cpu` 可回退）；也可用
`TORCH_DEVICE=auto|cpu|gpu|skip` 直接覆盖（`auto` = 构建期动态探测：本地 wheel >
基础镜像已含 torch > 构建机有 GPU > CPU）。打包与部署两侧须取同一取值，指纹才对得上。

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
