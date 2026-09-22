import hashlib
import json
import logging
import os
import re
from typing import Any, Dict, Optional

try:
    import docker
except ImportError:
    docker = None

from backend.shared.auth import get_internal_call_secret

logger = logging.getLogger(__name__)

# 编排模式唯一读取点（env `QM_ORCHESTRATION_MODE`）。只有两种取值：
#   docker（缺省）—— 与今天一致：容器编排就绪度按 Docker 引擎判；
#   none          —— 免容器节点（Windows 实盘节点包）显式声明「本机不做容器编排」。
#
# 为什么需要 none：实盘策略执行走的是**本机进程内沙箱**
# （real_trading_lifecycle `/start` → sandbox_manager.submit_strategy），
# `create_deployment`/`delete_deployment` 在整个代码库里没有被任何路径调用；
# Docker 只服务 AI-IDE 代码执行、minibt 回测与模型训练。缺省「按 Docker 判」
# 会让一台只用实盘的机器被准备度检测永久拦在启动之外（/start 复用同一份检测，
# 任一项不过即 409）。
ORCHESTRATION_MODE_ENV = "QM_ORCHESTRATION_MODE"
ORCHESTRATION_MODE_DOCKER = "docker"
ORCHESTRATION_MODE_NONE = "none"


def orchestration_mode() -> str:
    """本机编排模式；未配置/取值不认识时回 docker（缺省即今天的行为）。"""
    raw = str(os.getenv(ORCHESTRATION_MODE_ENV, "")).strip().lower()
    if raw == ORCHESTRATION_MODE_NONE:
        return ORCHESTRATION_MODE_NONE
    if raw and raw != ORCHESTRATION_MODE_DOCKER:
        logger.warning(
            "[Orchestration] %s=%r 取值不认识（只认 docker/none），按 docker 处理",
            ORCHESTRATION_MODE_ENV,
            raw,
        )
    return ORCHESTRATION_MODE_DOCKER


def orchestration_disabled() -> bool:
    """本机是否显式声明不使用容器编排（免容器节点）。"""
    return orchestration_mode() == ORCHESTRATION_MODE_NONE


class K8sManager:
    """
    Docker-based strategy runner manager (OSS Edition).
    Kubernetes support has been removed for OSS edition.
    """

    def __init__(self):
        self.mode = "docker"
        self.api = None
        self.core_api = None
        self.docker_client = None
        self._init_docker()

    def _init_docker(self):
        """Initialize Docker client for local/lightweight orchestration."""
        try:
            if docker:
                self.docker_client = docker.from_env()
                logger.info(
                    "Initialized Docker client for strategy orchestration (Docker Mode)"
                )
                self.api = True
                self.core_api = True
            else:
                logger.error("Docker SDK (docker-py) not installed")
                self.api = None
                self.core_api = None
        except Exception as e:
            logger.error(f"Failed to initialize Docker client: {e}")
            self.api = None
            self.core_api = None

    @staticmethod
    def _sanitize_k8s_name(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9-]+", "-", str(value or "").strip().lower())
        normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
        return normalized or "default"

    def _deployment_name(self, tenant_id: str, user_id: str) -> str:
        base = f"strategy-{self._sanitize_k8s_name(tenant_id)}-{self._sanitize_k8s_name(user_id)}"
        if len(base) <= 63:
            return base
        digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:8]
        return f"{base[:54].rstrip('-')}-{digest}"

    def create_deployment(
        self,
        user_id: str,
        strategy_file_path: str,
        run_id: str = "default",
        exec_config: dict = None,
        tenant_id: str = "default",
        live_trade_config: dict = None,
        strategy_id: str | None = None,
    ):
        """Create the strategy runtime in Docker or Kubernetes."""
        resolved_strategy_id = str(strategy_id or "").strip()
        if not resolved_strategy_id:
            resolved_strategy_id = (
                os.path.basename(strategy_file_path).replace(".py", "")
                if strategy_file_path
                else "default"
            )

        if self.mode == "docker":
            return self._create_docker_container(
                user_id,
                strategy_file_path,
                run_id,
                exec_config or {},
                tenant_id,
                live_trade_config or {},
                strategy_id=resolved_strategy_id,
            )
        return self._create_k8s_deployment(
            user_id,
            strategy_file_path,
            run_id,
            exec_config or {},
            tenant_id,
            live_trade_config or {},
            strategy_id=resolved_strategy_id,
        )

    def _create_docker_container(
        self,
        user_id: str,
        strategy_file_path: str,
        run_id: str,
        exec_config: dict,
        tenant_id: str,
        live_trade_config: dict,
        strategy_id: str,
    ):
        if not self.docker_client:
            return {"status": "error", "message": "Docker client not initialized"}

        name = self._deployment_name(tenant_id, user_id)
        # 默认使用通用实盘镜像
        image = os.getenv("STRATEGY_RUNNER_IMAGE", "quantmind-ml-runtime:latest")

        try:
            # Cleanup old container if exists
            try:
                old = self.docker_client.containers.get(name)
                logger.info(f"Removing existing container {name}")
                old.stop(timeout=5)
                old.remove()
            except Exception:
                pass

            # strategy_id is now explicitly passed

            env = {
                "USER_ID": str(user_id),
                "TENANT_ID": str(tenant_id or "default"),
                "STRATEGY_ID": strategy_id,
                "RUN_ID": str(run_id),
                "EXECUTION_CONFIG": json.dumps(exec_config or {}),
                "LIVE_TRADE_CONFIG": json.dumps(live_trade_config or {}),
                "INTERNAL_CALL_SECRET": get_internal_call_secret(),
                "TRADE_SERVICE_INTERNAL_URL": os.getenv(
                    "TRADE_SERVICE_INTERNAL_URL",
                    "http://quantmind-trade:8002/api/v1/internal/strategy",
                ),
                # runner/main.py 读取 ENGINE_SERVICE_INTERNAL_URL（保留 ENGINE_SERVICE_URL 兼容）
                "ENGINE_SERVICE_INTERNAL_URL": os.getenv(
                    "ENGINE_SERVICE_INTERNAL_URL",
                    "http://127.0.0.1:8001/api/v1",
                ),
                "ENGINE_SERVICE_URL": os.getenv(
                    "ENGINE_SERVICE_URL",
                    "http://127.0.0.1:8001",
                ),
                "PYTHONPATH": "/app",
                "TZ": os.getenv("TZ", "Asia/Shanghai"),
                "RUNNER_TIMEZONE": os.getenv("RUNNER_TIMEZONE", "Asia/Shanghai"),
                "REDIS_HOST": os.getenv("REDIS_HOST", "quantmind-redis"),
                "REDIS_PASSWORD": os.getenv("REDIS_PASSWORD", ""),
            }

            container = self.docker_client.containers.run(
                image=image,
                name=name,
                detach=True,
                environment=env,
                # Join the same network as trade-core to allow communication
                network="quantmind-network",
                restart_policy={"Name": "unless-stopped"},
                # Mount project dir for strategy code accessibility
                # If running inside docker, we need the HOST path of the project
                volumes={
                    os.getenv("HOST_PROJECT_PATH", os.getcwd()): {
                        "bind": "/app",
                        "mode": "rw",
                    }
                },
                mem_limit=os.getenv("STRATEGY_MEM_LIMIT", "1g"),
                command=[
                    "python",
                    "/app/backend/services/trade/runner/main.py",
                    "--user_id",
                    str(user_id),
                    "--strategy",
                    strategy_id,
                    "--tenant_id",
                    str(tenant_id or "default"),
                ],
            )

            logger.info(
                f"Successfully started Docker container {name} (ID: {container.short_id})"
            )
            return {"status": "success", "message": f"Container {name} started"}
        except Exception as e:
            logger.error(f"Failed to start Docker container {name}: {e}")
            return {"status": "error", "message": str(e)}

    def _create_k8s_deployment(
        self,
        user_id,
        strategy_file_path,
        run_id,
        exec_config,
        tenant_id,
        live_trade_config,
        strategy_id: str,
    ):
        if not self.api:
            return {"status": "error", "message": "K8s client not initialized"}

        name = self._deployment_name(tenant_id, user_id)
        exec_config = exec_config or {}
        live_trade_config = live_trade_config or {}

        # 使用我们在 Google Artifact Registry 创建的私有镜像
        image_path = os.getenv(
            "STRATEGY_RUNNER_IMAGE",
            "asia-east1-docker.pkg.dev/gen-lang-client-0953736716/quantmind-repo/quantmind-qlib-runner:latest",
        )

        # 定义容器资源限制 (多租户配额保护)
        resource_limits = {
            "cpu": os.getenv("STRATEGY_CPU_LIMIT", "500m"),  # 默认最大 0.5 核
            "memory": os.getenv("STRATEGY_MEM_LIMIT", "1Gi"),  # 默认最大 1GB 内存
        }
        resource_requests = {
            "cpu": os.getenv("STRATEGY_CPU_REQUEST", "100m"),  # 保证最少 0.1 核
            "memory": os.getenv("STRATEGY_MEM_REQUEST", "256Mi"),  # 保证最少 256MB 内存
        }

        # 定义容器
        container = client.V1Container(
            name=name,
            image=image_path,
            image_pull_policy="Always",
            command=["python", "/app/main.py"],
            args=["--strategy", strategy_id, "--user_id", user_id],
            resources=client.V1ResourceRequirements(
                limits=resource_limits, requests=resource_requests
            ),
            liveness_probe=client.V1Probe(
                exec=client.V1ExecAction(
                    command=[
                        "python",
                        "-c",
                        "import os, time; exit(0) if time.time() - os.getmtime('/tmp/heartbeat') < 120 else exit(1)",
                    ]
                ),
                initial_delay_seconds=30,
                period_seconds=30,
                failure_threshold=3,
            ),
            readiness_probe=client.V1Probe(
                exec=client.V1ExecAction(command=["ls", "/app/main.py"]),
                initial_delay_seconds=5,
                period_seconds=10,
            ),
            env=[
                client.V1EnvVar(name="USER_ID", value=user_id),
                client.V1EnvVar(name="TENANT_ID", value=str(tenant_id or "default")),
                client.V1EnvVar(name="STRATEGY_ID", value=strategy_id),
                client.V1EnvVar(name="RUN_ID", value=run_id),
                client.V1EnvVar(name="EXECUTION_CONFIG", value=json.dumps(exec_config)),
                client.V1EnvVar(
                    name="LIVE_TRADE_CONFIG", value=json.dumps(live_trade_config)
                ),
                client.V1EnvVar(
                    name="INTERNAL_CALL_SECRET", value=get_internal_call_secret()
                ),
                client.V1EnvVar(
                    name="TRADE_SERVICE_INTERNAL_URL",
                    value="http://quantmind-trade:8002/api/v1/internal/strategy",
                ),
                client.V1EnvVar(
                    name="ENGINE_SERVICE_URL",
                    value=os.getenv(
                        "ENGINE_SERVICE_URL", "http://127.0.0.1:8001"
                    ),
                ),
                client.V1EnvVar(
                    name="COS_BUCKET_URL",
                    value=os.getenv("COS_BUCKET_URL", "http://localhost:8000/uploads"),
                ),
            ],
        )

        template = client.V1PodTemplateSpec(
            metadata=client.V1ObjectMeta(
                labels={
                    "app": "quantmind-strategy",
                    "user": str(user_id),
                    "tenant": str(tenant_id or "default"),
                }
            ),
            spec=client.V1PodSpec(containers=[container]),
        )

        deployment = client.V1Deployment(
            api_version="apps/v1",
            kind="Deployment",
            metadata=client.V1ObjectMeta(
                name=name,
                labels={
                    "app": "quantmind-strategy",
                    "user": str(user_id),
                    "tenant": str(tenant_id or "default"),
                },
            ),
            spec=client.V1DeploymentSpec(
                replicas=1,
                selector=client.V1LabelSelector(
                    match_labels={
                        "app": "quantmind-strategy",
                        "user": str(user_id),
                        "tenant": str(tenant_id or "default"),
                    }
                ),
                template=template,
            ),
        )

        try:
            self.api.create_namespaced_deployment(
                namespace=self.namespace, body=deployment, _request_timeout=10
            )
            logger.info(f"Created K8s deployment {name}")
            return {"status": "success", "message": f"Deployment {name} created"}
        except ApiException as e:
            logger.error(f"K8s ApiException: {e}")
            return {"status": "error", "message": str(e)}

    def delete_deployment(self, user_id: str, tenant_id: str = "default"):
        """Delete the strategy runtime from Docker or Kubernetes."""
        if self.mode == "docker":
            return self._delete_docker_container(user_id, tenant_id)
        return self._delete_k8s_deployment(user_id, tenant_id)

    def _delete_docker_container(self, user_id: str, tenant_id: str):
        if not self.docker_client:
            return
        name = self._deployment_name(tenant_id, user_id)
        try:
            container = self.docker_client.containers.get(name)
            container.stop(timeout=5)
            container.remove()
            return {"status": "success", "message": f"Container {name} deleted"}
        except Exception:
            return {"status": "success", "message": "Container already gone"}

    def _delete_k8s_deployment(self, user_id: str, tenant_id: str = "default"):
        if not self.api:
            return {"status": "error", "message": "K8s client not initialized"}
        name = self._deployment_name(tenant_id, user_id)
        try:
            self.api.delete_namespaced_deployment(
                name=name,
                namespace=self.namespace,
                body=client.V1DeleteOptions(),
                _request_timeout=10,
            )
            logger.info(f"Deleted K8s deployment {name}")
            return {"status": "success", "message": f"Deployment {name} deleted"}
        except ApiException as e:
            if e.status == 404:
                return {"status": "success", "message": "Deployment already gone"}
            logger.error(f"K8s delete deployment failed: {e}")
            return {"status": "error", "message": str(e)}

    def get_status(self, user_id: str, tenant_id: str = "default"):
        """Gets status from Docker or K8s."""
        if self.mode == "docker":
            return self._get_docker_status(user_id, tenant_id)

        if not self.api:
            return {"status": "unknown", "message": "K8s client not initialized"}

        name = self._deployment_name(tenant_id, user_id)
        try:
            dep = self.api.read_namespaced_deployment(
                name=name, namespace=self.namespace, _request_timeout=5
            )
            return {
                "name": name,
                "replicas": dep.status.replicas,
                "ready_replicas": dep.status.ready_replicas,
                "available_replicas": dep.status.available_replicas,
                "unavailable_replicas": dep.status.unavailable_replicas,
            }
        except ApiException as e:
            if e.status == 404:
                return None
            return {"error": str(e)}
        except Exception as e:
            logger.error(f"K8s get_status error: {e}")
            return {"error": "Connection timeout"}

    def _get_docker_status(self, user_id: str, tenant_id: str):
        if not self.docker_client:
            return None
        name = self._deployment_name(tenant_id, user_id)
        try:
            container = self.docker_client.containers.get(name)
            is_running = container.status == "running"
            return {
                "name": name,
                "replicas": 1,
                "ready_replicas": 1 if is_running else 0,
                "available_replicas": 1 if is_running else 0,
                "unavailable_replicas": 0 if is_running else 1,
            }
        except Exception:
            return None

    def get_logs(self, user_id: str, tail: int = 100, tenant_id: str = "default"):
        """Gets logs from Docker or K8s."""
        if self.mode == "docker":
            return self._get_docker_logs(user_id, tail, tenant_id)

        if not self.core_api:
            return "K8s client not ready"

        try:
            label_selector = f"user={user_id},tenant={tenant_id}"
            pods = self.core_api.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=label_selector,
                _request_timeout=5,
            )
            if not pods.items:
                return "No running pods found."
            pod_name = pods.items[0].metadata.name
            return self.core_api.read_namespaced_pod_log(
                name=pod_name,
                namespace=self.namespace,
                tail_lines=tail,
                _request_timeout=10,
            )
        except Exception as e:
            return f"Error fetching K8s logs: {e}"

    def _get_docker_logs(self, user_id: str, tail: int, tenant_id: str):
        if not self.docker_client:
            return "Docker client not ready"
        name = self._deployment_name(tenant_id, user_id)
        try:
            container = self.docker_client.containers.get(name)
            return container.logs(tail=tail).decode("utf-8")
        except Exception as e:
            return f"Error fetching Docker logs: {e}"


k8s_manager = K8sManager()
