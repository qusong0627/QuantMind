# -*- coding: utf-8 -*-
"""minibt 模拟盘侧车生命周期（在交易进程内调用，经 docker.sock 起容器）。

只做三件事：落代码快照、起/停 ``quantmind-minibt-runner`` 容器、查容器存活。
计算与下单都不在这里——见 ``backend/shared/minibt_sim_agent.py`` 与
``simulation_hosted_scheduler``。

设计边界：
- 仅 OSS 单容器部署（``/var/run/docker.sock`` 已挂载）可用；远程薄节点
  无 docker 时 ``available()`` 返回 False，调用方必须**显式报错**，不得回落
  到沙箱（minibt 在沙箱里只会静默零信号）。
- 代码快照落在 ``/app/db/minibt_sim/...``（compose 的 ``./db`` 卷，容器重建
  不丢），重启恢复读快照文件，不依赖 active_strategy 里被截断的 code_str。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from backend.shared.minibt_sim_keys import (
    container_name,
    safe_segment,
)

RUNNER_IMAGE = os.getenv("AI_IDE_MINIBT_RUNNER_IMAGE", "quantmind-minibt-runner:latest")
DOCKER_NETWORK = os.getenv("AI_IDE_DOCKER_NETWORK", "quantmind_quantmind-net")
HOST_PROJECT_PATH = os.getenv("HOST_PROJECT_PATH", "/opt/quantmind")
RUNTIME_ROOT = os.getenv("QM_MINIBT_SIM_ROOT", "/app/db/minibt_sim")
_CONTAINER_LABEL = "qm.minibt.sim"

# 侧车只需要 Redis（db2 目标键 + 远端行情快照）与只读数据卷，不给 DB 凭据。
_PASSTHROUGH_ENV = (
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_PASSWORD",
    "REDIS_DB_TRADE",
    "REMOTE_QUOTE_REDIS_HOST",
    "REMOTE_QUOTE_REDIS_PORT",
    "REMOTE_QUOTE_REDIS_PASSWORD",
    "REMOTE_QUOTE_REDIS_DB",
    "REMOTE_QUOTE_DISABLED",
    "QM_QUANTDB_DATA_DIR",
)


class MinibtSimUnavailable(RuntimeError):
    """当前部署不具备 docker 能力，minibt 模拟盘不可启动。"""


def _docker_client():
    try:
        import docker
    except Exception as exc:  # noqa: BLE001 - 缺依赖=不可用，交给调用方显式报错
        raise MinibtSimUnavailable("docker_sdk_unavailable") from exc
    try:
        client = docker.from_env(timeout=10)
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001
        raise MinibtSimUnavailable("docker_unavailable") from exc


def runtime_dir(tenant_id: str, user_id: str, strategy_id: str) -> Path:
    return (
        Path(RUNTIME_ROOT)
        / safe_segment(tenant_id)
        / safe_segment(user_id)
        / safe_segment(strategy_id)
    )


def code_file(tenant_id: str, user_id: str, strategy_id: str) -> Path:
    return runtime_dir(tenant_id, user_id, strategy_id) / "strategy.py"


def _host_path(container_path: str) -> str:
    rel = os.path.relpath(container_path, "/app")
    return os.path.join(HOST_PROJECT_PATH, rel)


def _container_env(
    *, tenant_id: str, user_id: str, strategy_id: str, run_id: str
) -> dict[str, str]:
    env = {
        "PYTHONPATH": "/app",
        "PYTHONUNBUFFERED": "1",
        "TZ": "Asia/Shanghai",
        "QM_MINIBT_SIM_CODE": "/app/strategy.py",
        "QM_MINIBT_SIM_TENANT": str(tenant_id),
        "QM_MINIBT_SIM_USER": str(user_id),
        "QM_MINIBT_SIM_STRATEGY": str(strategy_id),
        "QM_MINIBT_SIM_RUN_ID": str(run_id),
        "QM_MINIBT_RESULT_DIR": "/tmp/minibt-sim-result",
        "QM_QUANTDB_DATA_DIR": os.getenv("QM_QUANTDB_DATA_DIR", "/data/quantdb"),
    }
    for key in _PASSTHROUGH_ENV:
        value = os.getenv(key)
        if value is not None and str(value).strip() != "":
            env[key] = str(value)
    return env


def _volumes(code_path: Path) -> dict[str, dict[str, str]]:
    return {
        _host_path(str(code_path)): {"bind": "/app/strategy.py", "mode": "ro"},
        os.path.join(HOST_PROJECT_PATH, "backend"): {
            "bind": "/app/backend",
            "mode": "ro",
        },
        os.path.join(HOST_PROJECT_PATH, "data"): {"bind": "/data", "mode": "ro"},
    }


class MinibtSimLauncher:
    """同步实现；异步调用方统一用 ``asyncio.to_thread`` 包一层。"""

    def available(self) -> bool:
        try:
            client = _docker_client()
        except MinibtSimUnavailable:
            return False
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
        return True

    def write_code(
        self, *, tenant_id: str, user_id: str, strategy_id: str, code: str
    ) -> Path:
        path = code_file(tenant_id, user_id, strategy_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".py.tmp")
        tmp.write_text(str(code), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def start(
        self,
        *,
        tenant_id: str,
        user_id: str,
        strategy_id: str,
        run_id: str,
        code: str,
    ) -> dict[str, Any]:
        client = _docker_client()
        try:
            # HOST_PROJECT_PATH 是**宿主机**路径（Docker daemon 侧解析），容器内
            # 看不到，不能 os.path.isdir 校验——与 AI-IDE 执行器同一约定，
            # 路径正确性由 compose/.env 保证。
            path = self.write_code(
                tenant_id=tenant_id,
                user_id=user_id,
                strategy_id=strategy_id,
                code=code,
            )
            name = container_name(tenant_id, user_id, strategy_id)
            self._remove_existing(client, name)
            from docker.errors import ImageNotFound

            try:
                container = client.containers.run(
                    RUNNER_IMAGE,
                    command=["python", "-u", "-m", "backend.shared.minibt_sim_agent"],
                    name=name,
                    detach=True,
                    volumes=_volumes(path),
                    network=DOCKER_NETWORK,
                    environment=_container_env(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        strategy_id=strategy_id,
                        run_id=run_id,
                    ),
                    labels={
                        _CONTAINER_LABEL: "1",
                        "qm.tenant": safe_segment(tenant_id),
                        "qm.user": safe_segment(user_id),
                        "qm.strategy": safe_segment(strategy_id),
                        "qm.run_id": str(run_id),
                    },
                    mem_limit="4g",
                    cpu_quota=100000,
                    pids_limit=256,
                    auto_remove=False,
                    restart_policy={"Name": "no"},
                )
            except ImageNotFound as exc:
                raise MinibtSimUnavailable(
                    f"runner_image_missing:{RUNNER_IMAGE}"
                ) from exc
            return {
                "runtime": "minibt",
                "container_id": container.id[:12],
                "container_name": name,
                "code_file": str(path),
            }
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self, *, tenant_id: str, user_id: str, strategy_id: str) -> bool:
        client = _docker_client()
        try:
            name = container_name(tenant_id, user_id, strategy_id)
            return self._remove_existing(client, name)
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def is_alive(self, *, tenant_id: str, user_id: str, strategy_id: str) -> bool:
        try:
            client = _docker_client()
        except MinibtSimUnavailable:
            return False
        try:
            container = client.containers.get(
                container_name(tenant_id, user_id, strategy_id)
            )
            container.reload()
            return str(getattr(container, "status", "")) in {"running", "restarting"}
        except Exception:  # noqa: BLE001 - NotFound 与不可用一律按不存活
            return False
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def read_code(
        self, *, tenant_id: str, user_id: str, strategy_id: str
    ) -> str | None:
        path = code_file(tenant_id, user_id, strategy_id)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def remove_code(self, *, tenant_id: str, user_id: str, strategy_id: str) -> None:
        """停止后删代码快照（含空目录）；删除失败不影响停止语义。"""
        directory = runtime_dir(tenant_id, user_id, strategy_id)
        try:
            shutil.rmtree(directory, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _remove_existing(client, name: str) -> bool:
        try:
            container = client.containers.get(name)
        except Exception:  # noqa: BLE001 - NotFound
            return False
        try:
            container.stop(timeout=10)
        except Exception:  # noqa: BLE001 - 已停止/已消失
            pass
        try:
            container.remove(force=True)
        except Exception:  # noqa: BLE001
            pass
        return True


minibt_sim_launcher = MinibtSimLauncher()
