# GKE 正式发布参数清单

更新时间：2026-03-06

本文档用于正式发布前收敛 `backend/scripts/gke_prod_preflight.sh` 与 [backend/scripts/gke_prod_release.sh](/Users/qusong/git/quantmind/backend/scripts/gke_prod_release.sh) 所需的真实参数。

当前已探测到的 GCP 项目现状：
- `PROJECT_ID=gen-lang-client-0953736716`
- 已存在测试集群：`quantmind-test-cluster`（`asia-east1-a`）
- 已存在测试 Cloud SQL：`quantmind-test-pg`
- 已存在 Artifact Registry：`asia-east1 / quantmind-repo`
- 已创建正式集群：`quantmind-prod-cluster`（`asia-east1-b`）
- 已创建正式 Cloud SQL：`quantmind-prod-pg`
- 已创建正式 Redis：`quantmind-prod-redis`（创建中）
- `redis.googleapis.com` 已启用

## 一、已确定参数

```bash
export PROJECT_ID="gen-lang-client-0953736716"
export REGISTRY_LOCATION="asia-east1"
export ARTIFACT_REPO="quantmind-repo"
export NAMESPACE="quantmind-prod"
export GKE_CLUSTER="quantmind-prod-cluster"
export GKE_ZONE="asia-east1-b"
```

镜像 tag 建议：

```bash
export IMAGE_TAG="release-20260306-1"
export TRADE_IMAGE_TAG="slim-20260306"
```

说明：
- `trade` 当前已验证通过的瘦身镜像 tag 是 `slim-20260306`
- 如果后续要保持 4 服务统一版本，可重新构建 `trade` 并推送成正式 release tag

## 二、待创建或待确认参数

### 1. GKE 正式集群

当前状态：已创建

建议值：

建议：
- 与测试集群分离
- 至少 2 个节点
- 节点需具备 Artifact Registry 拉取权限和 Cloud SQL Client 权限

### 2. Cloud SQL 正式实例

当前状态：已创建实例、数据库与应用用户

建议值：

```bash
export SQL_INSTANCE_CONNECTION_NAME="gen-lang-client-0953736716:asia-east1:quantmind-prod-pg"
export DB_NAME="quantmind"
export DB_USER="quantmind"
export DB_PASSWORD="<从 Secret Manager 读取 quantmind-prod-db-password>"
```

要求：
- 正式发布前完成备份/快照
- 发布前先完成 migration

已创建：
- Cloud SQL 实例：`quantmind-prod-pg`
- 数据库：`quantmind`
- 用户：`quantmind`
- 密码 Secret：`quantmind-prod-db-password`

### 3. Redis 正式实例

当前状态：
- 已启用 `redis.googleapis.com`
- 已创建 `quantmind-prod-redis`
- 当前状态：`READY`

两种可选路径：

路径 A：使用已创建的 Memorystore Redis

```bash
export REDIS_HOST="198.51.100.251"
export REDIS_PORT="6379"
export REDIS_PASSWORD=""
export REDIS_USE_SENTINEL="false"
export REDIS_SENTINELS=""
export REDIS_MASTER_NAME="quantmind-master"
```

路径 B：复用已有外部 Redis

```bash
export REDIS_HOST="<现有 Redis 地址>"
export REDIS_PORT="6379"
export REDIS_PASSWORD="<现有 Redis 密码>"
export REDIS_USE_SENTINEL="false|true"
export REDIS_SENTINELS="<sentinel1:26379,sentinel2:26379,sentinel3:26379>"
export REDIS_MASTER_NAME="quantmind-master"
```

### 4. CORS 与密钥

必须人工提供：

```bash
export CORS_ALLOWED_ORIGINS="https://app.quantmind.example,https://console.quantmind.example"
export INTERNAL_CALL_SECRET="<生产内部调用密钥>"
export SECRET_KEY="<生产应用密钥>"
export JWT_SECRET_KEY="<生产 JWT 密钥>"
```

要求：
- 不要使用测试值
- 不要使用 `latest` 风格的弱默认值

## 三、推荐正式发布导出模板

在正式环境变量确认后，可直接整理成：

```bash
export PROJECT_ID="gen-lang-client-0953736716"
export GKE_CLUSTER="quantmind-prod-cluster"
export GKE_ZONE="asia-east1-b"
export REGISTRY_LOCATION="asia-east1"
export ARTIFACT_REPO="quantmind-repo"
export NAMESPACE="quantmind-prod"

export IMAGE_TAG="release-20260306-1"
export TRADE_IMAGE_TAG="slim-20260306"

export DB_NAME="quantmind"
export DB_USER="quantmind"
export DB_PASSWORD="<从 Secret Manager 读取 quantmind-prod-db-password>"
export SQL_INSTANCE_CONNECTION_NAME="gen-lang-client-0953736716:asia-east1:quantmind-prod-pg"

export REDIS_HOST="198.51.100.251"
export REDIS_PORT="6379"
export REDIS_PASSWORD=""
export REDIS_USE_SENTINEL="false"
export REDIS_SENTINELS=""
export REDIS_MASTER_NAME="quantmind-master"

export CORS_ALLOWED_ORIGINS="https://app.quantmind.example,https://console.quantmind.example"
export INTERNAL_CALL_SECRET="<生产内部调用密钥>"
export SECRET_KEY="<生产应用密钥>"
export JWT_SECRET_KEY="<生产 JWT 密钥>"
```

## 四、正式发布前执行顺序

```bash
backend/scripts/gke_prod_preflight.sh
backend/scripts/gke_prod_release.sh
NAMESPACE=quantmind-prod backend/scripts/gke_smoke_check.sh
```

## 五、当前最实际的下一步

1. 创建或确认正式 GKE 集群
2. 创建或确认正式 Cloud SQL
3. 确认正式 Redis 路径
4. 准备正式密钥
5. 再跑 `gke_prod_preflight.sh`
