# QuantMind 部署指南

## 选择部署方式

| 方式 | 适用场景 | 入口 |
| --- | --- | --- |
| 完整部署 | 从 CDN 下载完整业务数据、模型与 Qlib 数据包，一键迁移；开箱即用 | `full-deploy.sh` |
| 在线源码部署 | 新服务器可稳定访问代码和镜像仓库，部署后另行准备数据 | `deploy.sh` |
| 一键更新 | 已部署服务器更新代码和核心服务 | `update.sh` |

所有脚本支持 Ubuntu 22.04 / 24.04，默认项目目录为 `/opt/quantmind`。

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
