"""
管理员 - 系统更新路由（web 一键更新 deploy/update.sh）
========================================================

POST /api/v1/admin/system/update            触发宿主 deploy/update.sh（分离执行）
GET  /api/v1/admin/system/update/status     查询更新进度（读取 data/update.log）

设计要点
--------
容器里没有宿主进程、也没有宿主机上的 .git/deploy 目录；但独立 OSS 部署的
main 容器已挂载 /var/run/docker.sock。因此这里借 **docker socket HTTP API**
拉一个「分离的 updater 容器」，让它在里面跑宿主的 deploy/update.sh：

  docker run --rm? --name quantmind-web-update \
    -v <project> : <project> :rw        -> 挂宿主真实项目目录(含 .git / deploy/)
    -v /var/run/docker.sock:/var/run/docker.sock
    -v <host-docker>:/usr/bin/docker:ro          -> 复用宿主 docker CLI
    -v <host-compose-plugin>:/usr/libexec/docker/cli-plugins:ro
    --entrypoint bash <app-image> -lc "bash <project>/deploy/update.sh > <project>/data/update.log 2>&1"

该容器不属于 compose 管理，deploy/update.sh 里 `docker compose up --force-recreate`
重建 main 服务时不会波及它，因此它能把整个更新（git pull → build → 重启 → 健康检查）
完整跑完 —— 这是「自升级自保」能实现的关键。

安全
----
功能默认开启；如需关闭设 QUANTMIND_ENABLE_WEB_UPDATE=false。开启且 docker socket
存在时才可用。挂载 docker.sock 的容器本就拥有宿主 root 级能力，故该接口：
  - 强校验 require_admin；
  - 可选 QUANTMIND_UPDATE_TOKEN，开启后必须携带匹配的 X-Update-Token；
  - 需要 ?confirm=1 显式确认。
所有路径都可经环境变量配置，见 BUILD/运行时说明。
"""

from __future__ import annotations

import logging
import os
import shlex
from io import StringIO
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query

from backend.services.api.user_app.middleware.auth import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])  # 路由器级认证兜底

# ---- 运行时配置（默认值面向 Ubuntu/Debian 宿主）----------------------
# 默认开启（web 控制台「更新系统」）；仅当显式设 false/0/no/off 时关闭。
def _enabled_from_env() -> bool:
    v = os.getenv("QUANTMIND_ENABLE_WEB_UPDATE", "").strip().lower()
    if v == "":
        return True  # 未设置 → 默认开启
    return v not in {"0", "false", "no", "off"}


_ENABLED = _enabled_from_env()
_TOKEN = os.getenv("QUANTMIND_UPDATE_TOKEN", "").strip()
_PROJECT_DIR = os.getenv("QUANTMIND_PROJECT_DIR", "/opt/quantmind")
_SOCKET = os.getenv("DOCKER_SOCKET_PATH", "/var/run/docker.sock")
# 宿主侧 docker CLI / compose 插件的路径 —— 由 docker daemon 在宿主侧解析。
_DOCKER_CLI = os.getenv("QUANTMIND_DOCKER_CLI", "/usr/bin/docker")
_COMPOSE_PLUGIN_DIR = os.getenv(
    "QUANTMIND_COMPOSE_PLUGIN_DIR", "/usr/libexec/docker/cli-plugins"
)
_SCRIPT = os.getenv("QUANTMIND_UPDATE_SCRIPT", "deploy/update.sh")
# API 容器内可读的更新日志（/data 挂载 === <project>/data）
_LOG_FILE = os.getenv("QUANTMIND_UPDATE_LOG", "/data/update.log")
_SCRIPT_PATH = os.path.join(_PROJECT_DIR, _SCRIPT)
_LOG_PATH = os.path.join(_PROJECT_DIR, "data", "update.log")
_CONTAINER_NAME = "quantmind-web-update"


def _docker_transport() -> httpx.HTTPTransport:
    return httpx.HTTPTransport(uds=_SOCKET)


def _docker_client() -> httpx.Client:
    return httpx.Client(transport=_docker_transport(), timeout=30.0)


def _enabled() -> bool:
    """功能开关：环境开启且 docker socket 存在。"""
    if not _ENABLED:
        return False
    return Path(_SOCKET).exists()


def _verify_token(token: str | None) -> None:
    if _TOKEN and token != _TOKEN:
        raise HTTPException(status_code=403, detail="更新令牌不匹配")


def _detect_image(client: httpx.Client) -> str:
    """用当前 main 容器镜像作为 updater 镜像；失败时回退环境变量/默认值。"""
    override = os.getenv("QUANTMIND_UPDATE_IMAGE", "").strip()
    if override:
        return override
    try:
        resp = client.get("http://localhost/containers/quantmind/json")
        if resp.status_code == 200:
            image = (resp.json().get("Config") or {}).get("Image")
            if image:
                return image
    except Exception:  # noqa: BLE001
        pass
    return "quantmind-oss:latest"


def _container_running(client: httpx.Client) -> bool:
    try:
        resp = client.get(f"http://localhost/containers/{_CONTAINER_NAME}/json")
        if resp.status_code == 200:
            return bool((resp.json().get("State") or {}).get("Running"))
    except Exception:  # noqa: BLE001
        pass
    return False


def _remove_stale(client: httpx.Client) -> None:
    """清理残留的旧 updater 容器（可能来自上次异常中断）。"""
    try:
        client.delete(
            f"http://localhost/containers/{_CONTAINER_NAME}",
            params={"force": 1, "v": 1},
        )
    except Exception:  # noqa: BLE001
        pass


def _build_container_spec(image: str) -> dict:
    cmd = f"bash {shlex.quote(_SCRIPT_PATH)} > {shlex.quote(_LOG_PATH)} 2>&1"
    binds = [
        f"{_PROJECT_DIR}:{_PROJECT_DIR}:rw",
        f"{_SOCKET}:/var/run/docker.sock",
        f"{_DOCKER_CLI}:/usr/bin/docker:ro",
        f"{_COMPOSE_PLUGIN_DIR}:/usr/libexec/docker/cli-plugins:ro",
    ]
    return {
        "Image": image,
        "Cmd": ["bash", "-lc", cmd],
        "WorkingDir": _PROJECT_DIR,
        "Env": [
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            f"QUANTMIND_PROJECT_DIR={_PROJECT_DIR}",
            f"DOCKER_CLI_PLUGINS={_COMPOSE_PLUGIN_DIR}",
        ],
        "HostConfig": {
            "Binds": binds,
            "AutoRemove": False,
        },
        "Labels": {"app": "quantmind-web-update"},
    }


@router.post("/update")
async def trigger_update(
    confirm: int = Query(default=0, ge=0, le=1),
    x_update_token: str | None = Header(default=None, alias="X-Update-Token"),
):
    """触发宿主 deploy/update.sh（分离 updater 容器，立即返回）。"""
    if not _enabled():
        raise HTTPException(
            status_code=403,
            detail=(
                "更新功能未开启：需设置 QUANTMIND_ENABLE_WEB_UPDATE=true "
                "并确保 docker socket 已挂载进容器。"
            ),
        )
    if confirm != 1:
        raise HTTPException(status_code=400, detail="缺少确认参数 confirm=1")
    _verify_token(x_update_token)

    try:
        client = _docker_client()
        if _container_running(client):
            raise HTTPException(status_code=409, detail="已有更新任务在执行中")

        image = _detect_image(client)
        spec = _build_container_spec(image)
        _remove_stale(client)

        created = client.post(
            "http://localhost/containers/create",
            params={"name": _CONTAINER_NAME},
            json=spec,
        )
        if created.status_code not in (201, 200):
            raise HTTPException(
                status_code=502, detail=f"创建 updater 容器失败: {created.text[:300]}"
            )
        cid = created.json().get("Id", "")
        # 注意：docker daemon 的 start 端点不带尾斜杠（带斜杠会 404）；204/304 均算成功。
        started = client.post(f"http://localhost/containers/{cid}/start")
        if started.status_code not in (204, 200, 304):
            raise HTTPException(
                status_code=502, detail=f"启动 updater 容器失败: {started.text[:300]}"
            )
        return {"success": True, "data": {"started": True, "task_id": cid}}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("trigger update failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"触发更新失败: {exc}") from exc


@router.get("/update/status")
async def update_status(
    x_update_token: str | None = Header(default=None, alias="X-Update-Token"),
):
    """查询更新任务状态：running / done / failed / idle。"""
    state: str = "idle"
    message = ""
    tail = ""

    # 容器仍在跑 => running（最高优先级）
    running = False
    try:
        client = _docker_client()
        running = _container_running(client)
    except Exception:  # noqa: BLE001
        running = False

    log_content = ""
    try:
        p = Path(_LOG_FILE)
        if p.exists():
            log_content = p.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    tail = log_content[-2000:]
    if running:
        state = "running"
    elif "更新完成" in log_content:
        state = "done"
        message = "系统更新完成"
    elif "健康检查失败" in log_content or "错误" in log_content:
        state = "failed"
        message = "系统更新失败"
    elif log_content:
        state = "failed"
        message = "更新中断，请查看日志"
    else:
        state = "idle"
        message = "尚未执行过更新"

    return {
        "success": True,
        "data": {
            "state": state,
            "message": message,
            "log_tail": tail,
        },
    }
