# QuantMind 源码包部署说明

本目录是 QuantMind OSS 源码，不保证包含业务数据、模型或 Qlib 数据。需要迁移完整业务环境时，请使用 CDN 发布的 `quantmind-offline` 离线包，而不是仅复制源码。

## 推荐方式：完整部署

完整部署包包含 Docker 镜像、PostgreSQL 业务备份、模型、业务文件和 Qlib 数据。在 Ubuntu 22.04 / 24.04 上执行：

```bash
curl -fsSL https://gitee.com/qusong0627/QuantMind/raw/master/deploy/full-deploy.sh | sudo bash
```

默认 CDN 地址为 `https://cdn.quantmind.cloud/quantmind-offline`。详情见 [deploy/README.md](deploy/README.md)。

## 源码手动部署

在线源码部署脚本（`deploy/deploy.sh` / `quick-deploy.sh`）已下线。若需从源码手动部署，请按 [部署指南 → 完全手动部署](../部署指南.md) 克隆代码、编写 `.env`，再用 `docker compose` 构建启动。默认目录为 `/opt/quantmind`。

## 服务入口

| 服务 | 默认地址 |
| --- | --- |
| Web | `http://<服务器 IP>:3000` |
| API 文档 | `http://<服务器 IP>:8000/docs` |
| Dashboard | `http://<服务器 IP>:8501` |
| QwenPaw | `http://<服务器 IP>:8088` |

## 更新与常用命令

```bash
cd /opt/quantmind
sudo bash deploy/update.sh
docker compose ps
docker compose logs -f quantmind
```

更新脚本默认保护数据库、Redis、模型、业务数据和 Qlib 数据；仅更新代码与核心容器。更多产品和开发说明请参阅根目录 [README.md](../../README.md)。
