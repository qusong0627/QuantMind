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
功能开关只看 **docker socket 是否存在**（`_enabled()`），QUANTMIND_ENABLE_WEB_UPDATE
与 QUANTMIND_UPDATE_TOKEN 已停用、不再参与判断 —— 前置一次 socket 检查只是为了
给出清晰报错。挂载 docker.sock 的容器本就拥有宿主 root 级能力，故该接口：
  - 强校验 require_admin；
  - 需要 docker socket 存在（不存在返回 403）。
路径由环境变量决定：QUANTMIND_PROJECT_DIR 指定项目目录（默认 /opt/quantmind），
QUANTMIND_REF / QUANTMIND_REMOTE 可选，透传给 deploy/update.sh；不设时 update.sh
默认更新到**当前 checkout 的分支**。
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

# ---- 运行时配置（已移除环境变量限制，固定默认值）----------------------
# 项目目录必须可配：容器实际跑在哪个 checkout 上，取决于部署方式。曾经硬编码
# /opt/quantmind 的后果是「更新系统」按钮直指一个不存在的路径，日志只留一行
# `bash: /opt/quantmind/deploy/update.sh: No such file or directory`，看不出是
# 路径配错。默认值保持 /opt/quantmind 以兼容既有部署。
_PROJECT_DIR = os.getenv("QUANTMIND_PROJECT_DIR", "/opt/quantmind")
_SOCKET = "/var/run/docker.sock"
_DOCKER_CLI = "/usr/bin/docker"
_COMPOSE_PLUGIN_DIR = "/usr/libexec/docker/cli-plugins"
_SCRIPT = "deploy/update.sh"
_LOG_FILE = "/data/update.log"
_SCRIPT_PATH = os.path.join(_PROJECT_DIR, _SCRIPT)
_LOG_PATH = os.path.join(_PROJECT_DIR, "data", "update.log")
_CONTAINER_NAME = "quantmind-web-update"


def _docker_transport() -> httpx.HTTPTransport:
    return httpx.HTTPTransport(uds=_SOCKET)


def _docker_client() -> httpx.Client:
    return httpx.Client(transport=_docker_transport(), timeout=30.0)


def _enabled() -> bool:
    """功能开关：仅校验 docker socket 存在，已移除环境变量限制。"""
    return Path(_SOCKET).exists()


def _verify_token(token: str | None) -> None:
    # 已移除 QUANTMIND_UPDATE_TOKEN 校验
    return


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
    # 存在性检查必须放在 **updater 容器内**做：那边按 {_PROJECT_DIR}:{_PROJECT_DIR}
    # 挂了整个项目目录，deploy/update.sh 才可见。API 容器只挂载了子目录
    # （backend/scripts/config/data/…，实测无 deploy/），在那儿检查会把正常部署
    # 全判成「脚本不存在」。检查结果写进 update.log —— 状态接口正是读它的尾部，
    # 这样配错目录时用户看到的是可操作的指引，而不是 bash 的一句 No such file。
    script = shlex.quote(_SCRIPT_PATH)
    guard = (
        f"if [ ! -f {script} ]; then "
        f'echo "[quantmind-update] 更新脚本不存在: {_SCRIPT_PATH}"; '
        f'echo "[quantmind-update] 请检查 QUANTMIND_PROJECT_DIR（当前 {_PROJECT_DIR}）'
        '是否指向宿主真实项目目录（需含 deploy/update.sh 与 .git）"; '
        "exit 1; fi; "
        f"exec bash {script}"
    )
    cmd = f"{{ {guard}; }} > {shlex.quote(_LOG_PATH)} 2>&1"
    binds = [
        f"{_PROJECT_DIR}:{_PROJECT_DIR}:rw",
        f"{_SOCKET}:/var/run/docker.sock",
        f"{_DOCKER_CLI}:/usr/bin/docker:ro",
        f"{_COMPOSE_PLUGIN_DIR}:/usr/libexec/docker/cli-plugins:ro",
    ]
    env = [
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        f"QUANTMIND_PROJECT_DIR={_PROJECT_DIR}",
        f"DOCKER_CLI_PLUGINS={_COMPOSE_PLUGIN_DIR}",
        "TZ=Asia/Shanghai",
        # 受信项目目录，避免 updater 容器内 git 因 UID 归属差异触发
        # dubious ownership 校验，导致所有 git 命令失败、被误判为"未提交改动"。
        # 见 GIT_CONFIG_COUNT 系列：https://git-scm.com/docs/git
        "GIT_CONFIG_COUNT=1",
        "GIT_CONFIG_KEY_0=safe.directory",
        f"GIT_CONFIG_VALUE_0={_PROJECT_DIR}",
    ]
    # 版本/远端透传：不设时 update.sh 自行「跟随当前 checkout 的分支」。
    # 设了才显式带过去，避免这里替运维决定分支。
    for var in ("QUANTMIND_REF", "QUANTMIND_REMOTE"):
        value = os.getenv(var, "").strip()
        if value:
            env.append(f"{var}={value}")
    return {
        "Image": image,
        "Cmd": ["bash", "-lc", cmd],
        "WorkingDir": _PROJECT_DIR,
        "Env": env,
        "HostConfig": {
            "Binds": binds,
            "AutoRemove": False,
        },
        "Labels": {"app": "quantmind-web-update"},
    }


@router.post("/update")
async def trigger_update(
    confirm: int = Query(default=1, ge=0, le=1),
    x_update_token: str | None = Header(default=None, alias="X-Update-Token"),
):
    """触发宿主 deploy/update.sh（分离 updater 容器，立即返回）。已移除环境变量与 confirm 强校验。"""
    if not _enabled():
        raise HTTPException(
            status_code=403,
            detail="docker socket 未挂载，无法触发更新",
        )

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
        # 异步记录“更新已触发”事件（失败不阻断主流程）
        try:
            from backend.shared.system_events import record_system_event_async
            import asyncio as _asyncio
            _asyncio.create_task(record_system_event_async(
                event_type="system_update",
                level="info",
                source="quantmind-api",
                title="系统更新已触发（Web）",
                message=f"updater 镜像 {image} 已启动，容器 {cid[:12]}",
                meta={"container_id": cid, "image": image},
            ))
        except Exception:
            pass
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
