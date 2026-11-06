"""Durable removal of terminal task workspaces, never database factor evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from pathlib import Path

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_path import owner_segment, user_scoped_dir
from backend.shared.owner_scope import OwnerScope

_TERMINAL = {"completed", "failed", "cancelled"}
_MAX_ENTRIES = 50_000


def _mount_id(fd: int) -> str:
    # Linux mount identity also distinguishes bind mounts on the same device.
    with open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as info:
        for line in info.read(4096).splitlines():
            if line.startswith("mnt_id:"):
                return line.split(":", 1)[1].strip()
    raise ArtifactCleanupConflict("无法核验任务目录挂载边界")


class ArtifactCleanupConflict(ValueError):
    """An execution, reference or workspace changed before cleanup."""


def _eligibility_sql(*, lock: bool = False):
    return text(
        "SELECT task.status,lease.state AS lease_state,"
        "EXISTS(SELECT 1 FROM rd_agent_factors f WHERE f.tenant_id=task.tenant_id "
        "AND f.user_id=task.user_id AND f.metadata_json->>'task_id'=task.task_id "
        "AND COALESCE(btrim(f.factor_code),'')='') AS missing_code,"
        "EXISTS(SELECT 1 FROM alpha_factor_jobs j "
        "LEFT JOIN resource_task_leases jl ON jl.pool=j.lease_pool "
        "AND jl.task_id=j.job_id AND jl.tenant_id=j.tenant_id AND jl.user_id=j.user_id "
        "WHERE j.tenant_id=task.tenant_id AND j.user_id=task.user_id "
        "AND j.job_id<>:cleanup_job AND j.kind<>'cleanup' "
        "AND (j.status IN ('queued','running','stopping') "
        "OR jl.state NOT IN ('completed','failed','cancelled')) "
        "AND (j.parent_task_id=task.task_id OR EXISTS(SELECT 1 FROM rd_agent_factors f "
        "WHERE f.tenant_id=task.tenant_id AND f.user_id=task.user_id "
        "AND f.metadata_json->>'task_id'=task.task_id "
        "AND (j.request_snapshot->'request'->>'factor_id'=f.factor_id "
        "OR j.request_snapshot->'request'->'factor_ids' ? f.factor_id "
        "OR j.request_snapshot->'request'->'factor_items' "
        "@> jsonb_build_array(jsonb_build_object('factor_id',f.factor_id)))))) AS busy "
        "FROM alpha_mining_tasks task LEFT JOIN resource_task_leases lease "
        "ON lease.pool='alpha_mining' AND lease.task_id=task.task_id "
        "AND lease.tenant_id=task.tenant_id AND lease.user_id=task.user_id "
        "WHERE task.tenant_id=:tenant_id AND task.user_id=:user_id "
        "AND task.task_id=:task_id" + (" FOR UPDATE OF task" if lock else "")
    )


def _check(row) -> None:
    if row is None:
        raise LookupError("任务不存在")
    if row["status"] not in _TERMINAL or row["lease_state"] not in _TERMINAL:
        raise ArtifactCleanupConflict("任务或执行租约尚未确认结束，不能清理")
    if row["missing_code"]:
        raise ArtifactCleanupConflict(
            "仍有未保存代码的因子，请先恢复代码再清理原工作区"
        )
    if row["busy"]:
        raise ArtifactCleanupConflict("仍有引用本次研究的活动作业，不能清理")


def _params(owner: OwnerScope, task_id: str, job_id: str = "") -> dict:
    return {**owner.sql_params(), "task_id": task_id, "cleanup_job": job_id}


def _marker_sql(*, lock: bool = False):
    return text(
        "SELECT job_id,state,root_identity,removed_bytes FROM alpha_task_artifact_cleanups "
        "WHERE tenant_id=:tenant_id AND user_id=:user_id AND task_id=:task_id"
        + (" FOR UPDATE" if lock else "")
    )


def _identity(info: os.stat_result) -> dict:
    return {"device": info.st_dev, "inode": info.st_ino}


def _owner_fd(root: Path, owner: OwnerScope) -> int | None:
    """Open each owner component independently; never follow directory symlinks."""
    scoped = user_scoped_dir(root, owner.tenant_id, owner.user_id)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = None
    try:
        fd = os.open(root, flags)
        for segment in scoped.relative_to(root).parts:
            child = os.open(segment, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except FileNotFoundError:
        if fd is not None:
            os.close(fd)
        return None
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise


def _walk(fd: int, *, remove: bool = False, evidence: dict | None = None) -> dict:
    digest = hashlib.sha256()
    count = 0
    size = 0
    observed = {}
    path_bytes = 0
    mount = _mount_id(fd)

    def visit(current: int, prefix: str, depth: int) -> None:
        nonlocal count, size, path_bytes
        if depth > 64:
            raise ArtifactCleanupConflict("目录层级超出清理限制")
        # A separate open file description prevents scandir from consuming the
        # traversal fd's shared directory offset before the deletion pass.
        listing = os.open(
            ".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
        )
        try:
            with os.scandir(listing) as entries:
                names = []
                for entry in entries:
                    count += 1
                    if count > _MAX_ENTRIES:
                        raise ArtifactCleanupConflict("目录项数量超出清理限制")
                    names.append(entry.name)
        finally:
            os.close(listing)
        for name in sorted(names):
            info = os.stat(name, dir_fd=current, follow_symlinks=False)
            relative = prefix + name
            path_bytes += len(os.fsencode(relative))
            if path_bytes > 8 * 1024 * 1024:
                raise ArtifactCleanupConflict("产物路径清单超出清理限制")
            signature = [
                info.st_mode,
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ]
            observed[relative] = signature
            # unlinking one hard link changes the remaining links' ctime. The
            # complete pre-delete inventory includes ctime; this pass compares
            # identity, mode, size and mtime without rejecting our own unlink.
            if remove and (
                evidence is None or evidence.get(relative, [])[:5] != signature[:5]
            ):
                raise ArtifactCleanupConflict("清理过程中产物发生变化，停止删除")
            digest.update(
                json.dumps(
                    [relative, *signature], ensure_ascii=True, separators=(",", ":")
                ).encode()
            )
            if stat.S_ISDIR(info.st_mode):
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current
                )
                try:
                    if _identity(os.fstat(child)) != _identity(info):
                        raise ArtifactCleanupConflict("目录在检查过程中发生变化")
                    if _mount_id(child) != mount:
                        raise ArtifactCleanupConflict(
                            "任务目录包含独立挂载，拒绝跨挂载清理"
                        )
                    visit(child, relative + "/", depth + 1)
                finally:
                    os.close(child)
                if remove:
                    if _identity(
                        os.stat(name, dir_fd=current, follow_symlinks=False)
                    ) != _identity(info):
                        raise ArtifactCleanupConflict("清理过程中子目录已被替换")
                    os.rmdir(name, dir_fd=current)
            else:
                # unlink removes a symlink itself; it cannot reach its target.
                if stat.S_ISREG(info.st_mode):
                    size += info.st_size
                if remove:
                    os.unlink(name, dir_fd=current)

    visit(fd, "", 0)
    return {
        "fingerprint": digest.hexdigest(),
        "size_bytes": size,
        "entries": count,
        "evidence": observed,
    }


def _workspace(
    root: Path, owner: OwnerScope, task_id: str, *, expected=None, remove: bool = False
) -> dict:
    if os.name != "posix":
        raise ArtifactCleanupConflict("产物清理必须由 Linux engine 执行")
    task_id = owner_segment(task_id)
    parent = _owner_fd(root, owner)
    empty = {
        "root_identity": {},
        "fingerprint": hashlib.sha256(b"").hexdigest(),
        "size_bytes": 0,
        "entries": 0,
    }
    if parent is None:
        return empty
    fd = None
    try:
        try:
            fd = os.open(
                task_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
        except FileNotFoundError:
            return empty
        identity = _identity(os.fstat(fd))
        if _mount_id(fd) != _mount_id(parent):
            raise ArtifactCleanupConflict("任务目录是独立挂载，拒绝清理")
        if expected is not None and identity != expected["root_identity"]:
            raise ArtifactCleanupConflict("任务工作区已替换，请重新预览")
        walked = _walk(fd)
        evidence = walked.pop("evidence")
        inventory = {"root_identity": identity, **walked}
        if expected is not None and inventory != expected:
            raise ArtifactCleanupConflict("任务产物已变化，请重新预览后清理")
        if remove:
            _walk(fd, remove=True, evidence=evidence)
            if (
                _identity(os.stat(task_id, dir_fd=parent, follow_symlinks=False))
                != identity
            ):
                raise ArtifactCleanupConflict("任务工作区在清理过程中被替换")
            os.rmdir(task_id, dir_fd=parent)
        return inventory
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _check_marker(marker, inventory: dict) -> None:
    if marker is None:
        return
    identity = marker["root_identity"]
    if isinstance(identity, str):
        identity = json.loads(identity)
    if marker["state"] == "deleted" and inventory["root_identity"]:
        raise ArtifactCleanupConflict("已清理任务目录重新出现，需管理员核对")
    if (
        marker["state"] == "deleting"
        and inventory["root_identity"]
        and inventory["root_identity"] != identity
    ):
        raise ArtifactCleanupConflict("中断清理的原目录已被替换，拒绝继续")


def lock_factor_origins(session, owner: OwnerScope, factor_ids: list[str]) -> None:
    """Serialize new factor jobs with cleanup's source-task admission lock."""
    if not factor_ids:
        return
    params = {**owner.sql_params(), "factor_ids": factor_ids}
    tasks = (
        session.execute(
            text(
                "SELECT task.task_id FROM alpha_mining_tasks task "
                "WHERE task.tenant_id=:tenant_id AND task.user_id=:user_id "
                "AND EXISTS(SELECT 1 FROM rd_agent_factors f "
                "WHERE f.tenant_id=task.tenant_id AND f.user_id=task.user_id "
                "AND f.metadata_json->>'task_id'=task.task_id "
                "AND f.factor_id=ANY(CAST(:factor_ids AS TEXT[]))) "
                "ORDER BY task.task_id FOR UPDATE OF task"
            ),
            params,
        )
        .scalars()
        .all()
    )
    if (
        tasks
        and session.execute(
            text(
                "SELECT 1 FROM alpha_task_artifact_cleanups gc "
                "LEFT JOIN resource_task_leases lease ON lease.pool='alpha_factor_jobs' "
                "AND lease.task_id=gc.job_id AND lease.tenant_id=gc.tenant_id "
                "AND lease.user_id=gc.user_id "
                "WHERE gc.tenant_id=:tenant_id AND gc.user_id=:user_id "
                "AND gc.task_id=ANY(CAST(:tasks AS TEXT[])) "
                "AND (lease.state IS NULL OR lease.state NOT IN ('completed','failed','cancelled')) LIMIT 1"
            ),
            {**params, "tasks": tasks},
        ).first()
    ):
        from backend.services.engine.alpha_agent.factor_visibility import (
            FactorVisibilityConflict,
        )

        raise FactorVisibilityConflict("原研究工作区正在清理，请等待清理结束再提交作业")


async def preview(root: Path, owner: OwnerScope, task_ids: list[str]) -> dict:
    root_binding = hashlib.sha256(os.path.abspath(root).encode()).hexdigest()
    items = []
    for task_id in sorted(task_ids):
        params = _params(owner, task_id)
        async with get_session() as session:
            await session.execute(text("SET LOCAL statement_timeout='5s'"))
            row = (await session.execute(_eligibility_sql(), params)).mappings().first()
            _check(row)
            marker = (await session.execute(_marker_sql(), params)).mappings().first()
        inventory = await asyncio.to_thread(_workspace, root, owner, task_id)
        _check_marker(marker, inventory)
        items.append({"task_id": task_id, **inventory})
    token = hashlib.sha256(
        json.dumps(
            {"owner": owner.sql_params(), "items": items, "root_binding": root_binding},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()
    return {
        "items": items,
        "preview_token": token,
        "root_binding": root_binding,
        "total_bytes": sum(item["size_bytes"] for item in items),
    }


def attach_cleanup(
    session, *, owner: OwnerScope, job_id: str, items: list[dict]
) -> None:
    """Tombstone admission is atomic with the durable job, lease and outbox."""
    for item in items:
        params = _params(owner, item["task_id"], job_id)
        _check(session.execute(_eligibility_sql(lock=True), params).mappings().first())
        marker = session.execute(_marker_sql(lock=True), params).mappings().first()
        _check_marker(
            marker, {key: value for key, value in item.items() if key != "task_id"}
        )
        if marker and marker["job_id"] != job_id:
            previous = session.execute(
                text(
                    "SELECT lease.state FROM resource_task_leases lease "
                    "WHERE lease.pool='alpha_factor_jobs' AND lease.tenant_id=:tenant_id "
                    "AND lease.user_id=:user_id AND lease.task_id=:previous"
                ),
                {**params, "previous": marker["job_id"]},
            ).scalar_one_or_none()
            if previous not in _TERMINAL:
                raise ArtifactCleanupConflict("该任务已有未结束的清理作业")
        session.execute(
            text(
                "INSERT INTO alpha_task_artifact_cleanups "
                "(tenant_id,user_id,task_id,job_id,state,root_identity) "
                "VALUES (:tenant_id,:user_id,:task_id,:cleanup_job,'pending',CAST(:identity AS jsonb)) "
                "ON CONFLICT (tenant_id,user_id,task_id) DO UPDATE SET "
                "job_id=EXCLUDED.job_id,updated_at=now(),root_identity=EXCLUDED.root_identity"
            ),
            {**params, "identity": json.dumps(item["root_identity"])},
        )


async def execute_cleanup(
    request: dict,
    *,
    root: Path,
    owner: OwnerScope,
    job_id: str,
    generation: int,
    jobs,
    lifecycle: dict,
) -> dict:
    from backend.services.engine.strategy_lab.runner.container_runner import (
        SandboxExecutionCancelled,
    )

    removed = []
    lifecycle["create_started"] = True
    try:
        if (
            request.get("root_binding")
            != hashlib.sha256(os.path.abspath(root).encode()).hexdigest()
        ):
            raise ArtifactCleanupConflict("产物根目录配置已变化，请重新预览")
        for item in request["items"]:
            current = await jobs.get(
                job_id,
                tenant_id=owner.tenant_id,
                user_id=owner.user_id,
                consistent=True,
            )
            if (
                current is None
                or current["status"] != "running"
                or current["execution_generation"] != generation
            ):
                raise SandboxExecutionCancelled("产物清理已停止，已完成部分保留审计")
            params = _params(owner, item["task_id"], job_id)
            async with get_session() as session:
                await session.execute(text("SET LOCAL lock_timeout='5s'"))
                running = (
                    await session.execute(
                        text(
                            "SELECT job_id FROM alpha_factor_jobs WHERE tenant_id=:tenant_id "
                            "AND user_id=:user_id AND job_id=:cleanup_job "
                            "AND execution_generation=:generation AND status='running' FOR UPDATE"
                        ),
                        {**params, "generation": generation},
                    )
                ).first()
                if running is None:
                    raise SandboxExecutionCancelled("清理作业已停止或代次已变化")
                _check(
                    (await session.execute(_eligibility_sql(lock=True), params))
                    .mappings()
                    .first()
                )
                marker = (
                    (await session.execute(_marker_sql(lock=True), params))
                    .mappings()
                    .first()
                )
                if marker is None or marker["job_id"] != job_id:
                    raise ArtifactCleanupConflict("清理作业归属已变化")
                if marker["state"] == "deleted":
                    removed.append(
                        {
                            "task_id": item["task_id"],
                            "removed_bytes": 0,
                            "already_deleted": True,
                        }
                    )
                    continue
                await session.execute(
                    text(
                        "UPDATE alpha_task_artifact_cleanups SET state='deleting',updated_at=now() "
                        "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                        "AND task_id=:task_id AND job_id=:cleanup_job"
                    ),
                    params,
                )
            inventory = {key: value for key, value in item.items() if key != "task_id"}
            operation = asyncio.create_task(
                asyncio.to_thread(
                    _workspace,
                    root,
                    owner,
                    item["task_id"],
                    expected=inventory,
                    remove=True,
                )
            )
            # Never let a cancelled HTTP controller abandon a live deletion thread.
            interrupted = False
            while True:
                try:
                    result = await asyncio.shield(operation)
                    break
                except asyncio.CancelledError:
                    interrupted = True
                    if operation.cancelled():
                        raise RuntimeError("清理线程回执丢失")
            async with get_session() as session:
                updated = await session.execute(
                    text(
                        "UPDATE alpha_task_artifact_cleanups SET state='deleted',updated_at=now(),"
                        "deleted_at=now(),removed_bytes=removed_bytes+:bytes "
                        "WHERE tenant_id=:tenant_id AND user_id=:user_id "
                        "AND task_id=:task_id AND job_id=:cleanup_job AND state='deleting'"
                    ),
                    {**params, "bytes": result["size_bytes"]},
                )
                if updated.rowcount != 1:
                    raise ArtifactCleanupConflict("清理完成记录已变化")
            removed.append(
                {"task_id": item["task_id"], "removed_bytes": result["size_bytes"]}
            )
            await jobs.update_progress(
                job_id,
                tenant_id=owner.tenant_id,
                user_id=owner.user_id,
                generation=generation,
                progress={
                    "stage": "清理任务产物",
                    "items": removed,
                    "progress_pct": round(100 * len(removed) / len(request["items"])),
                },
            )
            if interrupted:
                await jobs.request_cancel(
                    job_id,
                    tenant_id=owner.tenant_id,
                    user_id=owner.user_id,
                    generation=generation,
                )
                raise SandboxExecutionCancelled(
                    "控制请求中断；当前目录已清理并记录，其余任务未删除"
                )
        return {
            "items": removed,
            "removed_bytes": sum(item["removed_bytes"] for item in removed),
            "note": "任务、不可变规格/结果、因子代码/版本及训练引用保留；文件不可恢复。",
        }
    finally:
        lifecycle["confirmed_stopped"] = True
