# GKE 正式发布 Runbook

本文档固化 QuantMind 后端 4 核心服务在 GKE + Cloud SQL PostgreSQL + 外部 Redis 上的正式发布流程。

适用范围：
- `quantmind-api`
- `quantmind-engine`
- `quantmind-trade`
- `quantmind-stream`

对应模板：
- [k8s/quantmind-prod-release.yaml](/Users/qusong/git/quantmind/k8s/quantmind-prod-release.yaml)

对应脚本：
- [backend/scripts/gke_prod_preflight.sh](/Users/qusong/git/quantmind/backend/scripts/gke_prod_preflight.sh)
- [backend/scripts/gke_prod_release.sh](/Users/qusong/git/quantmind/backend/scripts/gke_prod_release.sh)
- [backend/scripts/gke_smoke_check.sh](/Users/qusong/git/quantmind/backend/scripts/gke_smoke_check.sh)

参数清单：
- [backend/DOCS/gke_prod_parameter_sheet.md](/Users/qusong/git/quantmind/backend/DOCS/gke_prod_parameter_sheet.md)

## 前置条件

本机要求：
- 已安装并登录 `gcloud`
- 已安装 `kubectl`
- 已安装 Docker，且已启用 `buildx`
- 已存在项目根目录 `.venv`

GCP 要求：
- 已启用 `container.googleapis.com`
- 已启用 `artifactregistry.googleapis.com`
- 已启用 `sqladmin.googleapis.com`
- GKE 节点服务账号具备 `roles/cloudsql.client`
- 节点具备拉取 Artifact Registry 镜像的权限

运行基线：
- 正式 Redis 已就绪，禁止继续使用测试环境里的集群内临时 Redis
- Cloud SQL 正式实例已就绪
- 正式密钥来自 Secret Manager / CI Secret / K8s Secret，不从仓库明文读取

## 推荐环境变量

```bash
export PROJECT_ID="your-gcp-project"
export GKE_CLUSTER="quantmind-prod-cluster"
export GKE_ZONE="asia-east1-b"
export REGISTRY_LOCATION="asia-east1"
export ARTIFACT_REPO="quantmind-repo"
export NAMESPACE="quantmind-prod"

export IMAGE_TAG="release-20260306-1"
export TRADE_IMAGE_TAG="release-20260306-1"

export DB_NAME="quantmind"
export DB_USER="quantmind"
export DB_PASSWORD="replace-with-prod-db-password"
export SQL_INSTANCE_CONNECTION_NAME="your-project:asia-east1:quantmind-prod-pg"

export REDIS_HOST="198.51.100.15"
export REDIS_PORT="6379"
export REDIS_PASSWORD="replace-with-prod-redis-password"
export REDIS_USE_SENTINEL="false"
export REDIS_SENTINELS=""
export REDIS_MASTER_NAME="quantmind-master"

export CORS_ALLOWED_ORIGINS="https://app.quantmind.example,https://console.quantmind.example"

export INTERNAL_CALL_SECRET="replace-with-prod-internal-secret"
export SECRET_KEY="replace-with-prod-secret"
export JWT_SECRET_KEY="replace-with-prod-jwt-secret"
```

说明：
- 正式发布禁止直接依赖 `latest`；`IMAGE_TAG` 必须显式传入。
- `TRADE_IMAGE_TAG` 不传时默认回退到 `IMAGE_TAG`。
- `CORS_ORIGINS_JSON` 不传时，脚本会根据 `CORS_ALLOWED_ORIGINS` 自动生成 JSON 数组。
- 如果需要通过本机 Electron/Vite 开发服务器联调正式后端，需临时将以下源加入 `CORS_ALLOWED_ORIGINS`：
  - `http://127.0.0.1:3000`
  - `http://localhost:3000`
  - `http://127.0.0.1:5173`
  - `http://localhost:5173`
- 如果 Redis 走 Sentinel，将 `REDIS_USE_SENTINEL=true`，并传入逗号分隔的 `REDIS_SENTINELS=host1:26379,host2:26379,host3:26379`。

## 1. 构建并推送正式镜像

推荐使用固定 release tag：

```bash
gcloud auth configure-docker "${REGISTRY_LOCATION}-docker.pkg.dev" --quiet

docker buildx build --platform linux/amd64 \
  -f backend/services/api/Dockerfile \
  -t "${REGISTRY_LOCATION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/quantmind-api:${IMAGE_TAG}" \
  --push .

docker buildx build --platform linux/amd64 \
  -f backend/services/engine/Dockerfile \
  -t "${REGISTRY_LOCATION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/quantmind-engine:${IMAGE_TAG}" \
  --push .

docker buildx build --platform linux/amd64 \
  -f backend/services/trade/Dockerfile \
  -t "${REGISTRY_LOCATION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/quantmind-trade:${TRADE_IMAGE_TAG:-$IMAGE_TAG}" \
  --push .

docker buildx build --platform linux/amd64 \
  -f backend/services/stream/Dockerfile \
  -t "${REGISTRY_LOCATION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPO}/quantmind-stream:${IMAGE_TAG}" \
  --push .
```

建议：
- `trade` 保持独立 tag 能力，便于单独热修和内存风险控制。
- 不要把正式回滚建立在 `latest` 上。

## 2. 数据库与迁移

正式环境不要继续依赖测试 SQL 基线文件直接初始化。

要求：
- 在发布前完成 schema migration
- 明确数据备份与回滚点
- 验证应用账号权限

发布前至少确认：
- Cloud SQL 备份/快照已完成
- migration 已在预发验证
- 回滚 SQL 或恢复方案已准备

## 3. 渲染与发布

正式发布前，建议先跑预检：

```bash
backend/scripts/gke_prod_preflight.sh
```

预检会校验：
- 必填环境变量是否齐全
- GKE 集群凭据是否可用
- Artifact Registry 中 4 个目标镜像 tag 是否存在
- 正式模板能否成功渲染
- 渲染产物中的关键字段是否正确

如只想预览渲染结果：

```bash
RENDER_ONLY=1 backend/scripts/gke_prod_release.sh
```

正式发布：

```bash
backend/scripts/gke_prod_release.sh
```

脚本会自动完成：
- 获取集群凭据
- 渲染 [k8s/quantmind-prod-release.yaml](/Users/qusong/git/quantmind/k8s/quantmind-prod-release.yaml)
- `kubectl apply`
- 等待 4 个核心 Deployment 全部就绪

说明：
- 正式模板默认不再创建 Redis Deployment，要求外部 Redis 已就绪。
- 默认副本数：
  - `api=2`
  - `engine=1`
  - `trade=2`
  - `stream=2`
- `trade` 模板会在应用进程启动前先等待本地 `127.0.0.1:5432` 可连通，避免 Cloud SQL Proxy 尚未就绪时把服务健康状态固定成 `degraded`
- 如需覆盖，使用：
  - `API_REPLICAS`
  - `ENGINE_REPLICAS`
  - `TRADE_REPLICAS`
  - `STREAM_REPLICAS`

## 4. 发布后验收

执行：

```bash
NAMESPACE="${NAMESPACE}" backend/scripts/gke_smoke_check.sh
```

必须额外人工确认：
- `kubectl get pods -n ${NAMESPACE}` 无 `CrashLoopBackOff`
- `kubectl top pod -n ${NAMESPACE}` 资源使用稳定
- 监控与告警已接通
- 日志中无持续 DB/Redis 鉴权失败

## 5. 建议灰度顺序

建议分 4 步：
1. 正式环境部署，但暂不切公网入口
2. 内部 smoke 与资源观测
3. 小流量灰度
4. 全量切换

不要直接从测试环境结论跳到正式全量。

## 6. 回滚

应用级回滚：

```bash
kubectl rollout undo deployment/quantmind-api -n "${NAMESPACE}"
kubectl rollout undo deployment/quantmind-engine -n "${NAMESPACE}"
kubectl rollout undo deployment/quantmind-trade -n "${NAMESPACE}"
kubectl rollout undo deployment/quantmind-stream -n "${NAMESPACE}"
```

镜像级回滚：
- 将 Deployment image 切回上一个 release tag
- `trade` 保留独立 tag，避免拖累其他服务一并回退

数据级回滚：
- 使用 Cloud SQL 备份/快照恢复
- 不接受“无快照直接上线”

## 7. 常见问题

### 1. `ImagePullBackOff`

优先检查：
- `IMAGE_TAG` / `TRADE_IMAGE_TAG` 是否已推送
- GKE 节点权限是否能拉取 Artifact Registry

### 2. `trade` 再次出现 `OOMKilled`

优先检查：
- 是否仍在使用瘦身版镜像
- `limits.memory=2Gi` 是否被改小
- 节点是否有镜像冷拉取抖动

### 3. Redis 连通失败

优先检查：
- `REDIS_HOST/REDIS_PORT/REDIS_PASSWORD`
- Sentinel 模式下 `REDIS_USE_SENTINEL/REDIS_SENTINELS/REDIS_MASTER_NAME`
- 网络策略与 VPC 连通性

### 4. CORS 被拒绝

优先检查：
- `CORS_ALLOWED_ORIGINS` 是否包含正式前端域名
- 是否误传 `*`

## 8. 最低发布门禁

建议把下面这些视为正式发布前必过项：
- 当前测试环境 smoke 已通过
- `backend/scripts/p2_ci_quality_gate.py` 通过
- 4 服务基础测试全绿
- 发布模板已渲染检查
- 回滚命令已演练
- Cloud SQL 备份已完成
