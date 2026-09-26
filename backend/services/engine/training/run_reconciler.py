"""启动对账：后端重启后处理被「看丢」的远程训练任务。

背景：远程训练的进度轮询是 API 进程内的 asyncio 协程
（``remote_ssh_orchestrator.launch_training_job`` → ``REGISTRY.register(_poll_remote)``）。
容器重启会杀掉该协程，而远端训练进程仍在节点上继续跑，于是 run 会永久卡在
``running``：产物不回传、模型不注册、前端一直显示「后端可能正在重启」。

本模块在 API lifespan 里对账一次，按远端真实状态收敛：

- 远端进程仍存活 → 重新挂载进度轮询（继续流式日志与收尾）
- 进程已结束且 exit=0 → 走收尾（拉产物 + 触发模型注册）
- 进程已结束且 exit!=0 → 标记 failed
- SSH 连续不可达 → 标记 failed 并写明原因（不永久卡 running）

对账只处理 ``status='running'`` 且 ``node_id`` 非 local 的任务；每次动作前重新
读一次状态，已终态则跳过（幂等）。任何异常都不影响启动。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

# SSH 探测重试：重启瞬间节点可能刚好短暂不可达，避免误判为失败
_SSH_ATTEMPTS = 3
_SSH_RETRY_SLEEP_SEC = 10

_reconcile_task: asyncio.Task | None = None


def start_run_reconciler() -> asyncio.Task:
    """在 API lifespan 中启动一次启动对账；已运行则复用。"""
    global _reconcile_task
    if _reconcile_task is None or _reconcile_task.done():
        _reconcile_task = asyncio.create_task(_run_once())
    return _reconcile_task


async def stop_run_reconciler() -> None:
    global _reconcile_task
    if _reconcile_task:
        _reconcile_task.cancel()
        try:
            await _reconcile_task
        except asyncio.CancelledError:
            pass
        _reconcile_task = None


async def _run_once() -> dict[str, Any]:
    """对账一轮，返回统计信息（同时写日志，便于事后排查）。"""
    stats = {"scanned": 0, "reattached": 0, "completed": 0, "failed": 0, "skipped": 0}
    try:
        jobs = await _load_running_jobs()
    except Exception as exc:  # noqa: BLE001
        logger.warning("run reconciler: 读取运行中任务失败: %s", exc)
        return stats

    stats["scanned"] = len(jobs)
    if not jobs:
        return stats

    logger.info("run reconciler: 发现 %d 个 running 远程训练任务，开始对账", len(jobs))
    for run_id, node_id in jobs:
        try:
            outcome = await _reconcile_one(run_id, node_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("run reconciler: %s 对账异常: %s", run_id, exc, exc_info=True)
            outcome = "skipped"
        stats[outcome] = stats.get(outcome, 0) + 1

    logger.info(
        "run reconciler 完成: 扫描=%d 重挂=%d 收尾=%d 标记失败=%d 跳过=%d",
        stats["scanned"],
        stats["reattached"],
        stats["completed"],
        stats["failed"],
        stats["skipped"],
    )
    return stats


async def _load_running_jobs() -> list[tuple[str, str]]:
    """返回 [(run_id, node_id)]，只含 status='running' 的远程节点任务。"""
    import sqlalchemy as sa

    from backend.services.api.routers.admin.db import TrainingJobRecord
    from backend.shared.database_manager_v2 import get_session

    async with get_session() as db:
        rows = (
            await db.execute(
                sa.select(
                    TrainingJobRecord.id,
                    TrainingJobRecord.request_payload,
                ).where(TrainingJobRecord.status == "running")
            )
        ).all()

    jobs: list[tuple[str, str]] = []
    for run_id, payload in rows:
        node_id = ""
        if isinstance(payload, dict):
            node_id = str(payload.get("node_id") or "").strip()
        if node_id and node_id != "local":
            jobs.append((str(run_id), node_id))
    return jobs


async def _load_job_identity(run_id: str) -> tuple[str, str]:
    """取 run 的 (tenant_id, user_id)，供补写 Redis 日志时归属正确。"""
    from backend.services.api.routers.admin.db import TrainingJobRecord
    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session() as db:
            record = await db.get(TrainingJobRecord, run_id)
            if record is None:
                return "default", ""
            return str(record.tenant_id or "default"), str(record.user_id or "")
    except Exception:  # noqa: BLE001
        return "default", ""


async def _is_still_running(run_id: str) -> bool:
    """动作前重读状态，保证幂等（已终态就不再处理）。"""
    from backend.services.api.routers.admin.db import TrainingJobRecord
    from backend.shared.database_manager_v2 import get_session

    async with get_session() as db:
        record = await db.get(TrainingJobRecord, run_id)
        return bool(record) and str(record.status or "") == "running"


def _build_probe(orch: Any, run_id: str) -> str:
    """构造远端存活探测命令，输出必须带 ===META=== 以通过 _ssh_probe_failed。"""
    work = str(orch.work_dir).rstrip("/")
    if getattr(orch, "exec_mode", "") == "native_python":
        pid_file = f"{work}/train_{run_id}.pid"
        exit_mark = f"{work}/train_{run_id}.exit"
        return (
            f"pid=$(cat {pid_file} 2>/dev/null || true); "
            f"echo '===META==='; "
            f"echo PID:$pid; "
            f'if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then echo ALIVE:1; '
            f"else echo ALIVE:0; fi; "
            f"echo EXIT:$(cat {exit_mark} 2>/dev/null || true)"
        )
    container = f"qm-train-{run_id}"
    return (
        f"echo '===META==='; "
        f"echo RUNNING:$(docker inspect -f '{{{{.State.Running}}}}' {container} 2>/dev/null || echo missing); "
        f"echo EXIT:$(docker inspect -f '{{{{.State.ExitCode}}}}' {container} 2>/dev/null || true)"
    )


def _parse_meta(out: str) -> dict[str, str]:
    _, _, meta_blob = out.partition("===META===")
    meta: dict[str, str] = {}
    for line in meta_blob.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            meta[key.strip()] = val.strip()
    return meta


async def _reconcile_one(run_id: str, node_id: str) -> str:
    """对账单个任务，返回 reattached / completed / failed / skipped。"""
    from backend.services.engine.training.orchestrator_base import (
        REGISTRY,
        get_orchestrator,
    )

    if not await _is_still_running(run_id):
        return "skipped"

    try:
        orch = get_orchestrator(node_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("run reconciler: %s 无法构建节点 %s 的编排器: %s", run_id, node_id, exc)
        return "skipped"

    tenant_id, user_id = await _load_job_identity(run_id)
    # 新构造的编排器不知道租户/用户，补上后 Redis 日志才能归属到正确的用户
    orch._tenant_id = tenant_id
    orch._user_id = user_id

    probe = _build_probe(orch, run_id)
    out = ""
    failed = ""
    for attempt in range(1, _SSH_ATTEMPTS + 1):
        try:
            code, out, err = await orch._ssh_exec(probe, timeout=120)
            failed = orch._ssh_probe_failed(code, out, err) or ""
        except Exception as exc:  # noqa: BLE001
            failed = f"{type(exc).__name__}: {exc}"
            out = ""
        if not failed:
            break
        logger.warning(
            "run reconciler: %s 探测节点失败（%d/%d）: %s",
            run_id,
            attempt,
            _SSH_ATTEMPTS,
            failed,
        )
        if attempt < _SSH_ATTEMPTS:
            await asyncio.sleep(_SSH_RETRY_SLEEP_SEC)

    if failed:
        # 节点不可达：无法确认远端状态，按用户约定标记失败而不是永久卡 running
        orch._log(
            run_id,
            f"[ERROR] 后端重启后对账无法连接训练节点 {node_id}（已重试 {_SSH_ATTEMPTS} 次）："
            f"{str(failed)[:180]}。远端可能仍在运行，请到节点上确认。",
            status="failed",
            progress=0,
        )
        await asyncio.sleep(0.5)  # 让 _persist_status 的 create_task 落地
        return "failed"

    meta = _parse_meta(out)
    is_native = getattr(orch, "exec_mode", "") == "native_python"

    if is_native:
        alive = meta.get("ALIVE") == "1"
        exit_code = meta.get("EXIT") or "1"
        run_key = f"native-{run_id}"
    else:
        alive = meta.get("RUNNING") == "true"
        exit_code = meta.get("EXIT") or "1"
        run_key = f"qm-train-{run_id}"

    if alive:
        # 远端仍在跑：重新挂载轮询（跳过已回放的旧日志），并告知用户
        orch._log(
            run_id,
            "[SYSTEM] 后端重启导致进度轮询中断，已自动重新挂载轮询（训练未受影响）",
            status="running",
            progress=50,
        )
        REGISTRY.register(
            orch._poll_remote(run_id, run_key, skip_existing_log=True),
            run_id=run_id,
        )
        logger.info("run reconciler: %s 远端仍在运行，已重新挂载轮询", run_id)
        return "reattached"

    # 进程/容器已结束：按退出码收尾（拉产物 + 触发注册，或标记失败）
    orch._log(
        run_id,
        f"[SYSTEM] 后端重启后对账发现远端训练已结束 (exit={exit_code})，开始收尾",
        status="running",
        progress=90,
    )
    await orch._handle_container_end(run_id, run_key, exit_code)
    await asyncio.sleep(0.5)
    logger.info("run reconciler: %s 已收尾 (exit=%s)", run_id, exit_code)
    return "completed" if exit_code == "0" else "failed"
