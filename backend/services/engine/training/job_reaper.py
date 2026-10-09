"""训练僵尸作业判尸与回收（P0-3）：reconcile_training_jobs()。

背景（docs/滚动训练与模型生命周期_设计方案.md §4.4，实测）：`recover_pending_runs`
（orchestrator_base.py）无生产调用方 → API 重启后 `admin_training_jobs` 的
pending/provisioning/running/waiting_callback 行**永挂**（容器已死/被删，
前端永远「训练中」）。本模块把「判尸」接上线：

纪律（判尸与重投分离）：
- **不动活作业**：在管 task（REGISTRY.is_active）/ 容器存活 / 进程存活 → 不碰；
- **可验证存活 → 重挂**（reattach）：容器仍在跑（重启前启动的孤儿），
  以既有 _poll_container 语义恢复监管（exit0 → 等回调；异常 → failed；
  超预算 → kill），**不重投**、不消耗新算力；
- **判死 → mark_failed(reason=orphaned…) + 释放单飞锁 + 告警**；默认不自动重投；
- Remote（AutoDL）本机不可验证 → 一律跳过并计数（P0 范围外）。

判定规则（classify_job，纯函数）：
- docker：容器 `qm-train-{run_id}` 状态 running/created/paused/exited → reattach；
  NotFound（missing）→ pending/provisioning 年轻（默认 600s，防建容器窗口
  误杀）跳过，否则判死；docker daemon 不可达（unavailable）→ 预算内跳过
  （防 daemon 抖动误杀），超过 1.5×max_time+回调宽限 → 判死；
- process：PID 存活 → 跳过（回调到达时自会收敛；进程仍在训练）；PID 死 →
  判死（例外：pid 从未记录且行年轻 pending/provisioning = 启动窗口，
  跳过）；pid 不可读且不年轻 → 预算内跳过、超预算判死；
- 任何模式超过 1.5×max_time_minutes + 回调宽限仍不可验证 → 判死。

调度：API 进程启动期立即扫一遍 + 每小时循环（main.py lifespan 启停），
心跳 `qm:sched:hb:training_reaper` 进 C07 体检；开关 `TRAINING_JOB_REAPER_ENABLED`
（缺省开）。手动演练：``python -m backend.services.engine.training.job_reaper --dry-run``。
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

REAPABLE_STATUSES: tuple[str, ...] = (
    "pending",
    "provisioning",
    "running",
    "waiting_callback",
)

DEFAULT_PENDING_GRACE_SECONDS = 600  # 无资源宽限（建容器/写 pid 窗口）
DEFAULT_CALLBACK_GRACE_SECONDS = 900  # 回调超时 600s + 余量
BUDGET_AGE_MULTIPLIER = 1.5  # 与方案文档一致：1.5×max_time 仍不可验证 → 判死

# 年轻宽限适用状态：这两个状态下资源可能尚在 provisioning（容器未建完 /
# pid 未回写），探针「不存在」不代表死亡（复审 MEDIUM-5，2026-10-08）
YOUNG_GRACE_STATUSES: tuple[str, ...] = ("pending", "provisioning")

_DOCKER_PRESENT_STATES = ("running", "created", "paused", "exited")
_CONTAINER_NAME_FMT = "qm-train-{run_id}"

_RUN_JOB_DONE_MARK = "[REAPER]"


@dataclass(frozen=True)
class JobView:
    """判尸输入快照（纯数据；由收集器从 DB/探针组装）。"""

    run_id: str
    status: str
    tenant_id: str = "default"
    user_id: str = "unknown"
    instance_id: str | None = None
    age_seconds: float = 0.0  # now - coalesce(updated_at, created_at)，naive UTC 同源
    max_time_minutes: int = 120
    mode: str = "docker"  # docker | process | remote
    probe: str = "unavailable"  # docker: running|created|paused|exited|missing|unavailable；process: alive|dead
    in_registry: bool = False


@dataclass(frozen=True)
class Verdict:
    action: str  # skip | reattach | mark_failed
    reason: str


def classify_job(
    view: JobView,
    *,
    pending_grace_seconds: float = DEFAULT_PENDING_GRACE_SECONDS,
    callback_grace_seconds: float = DEFAULT_CALLBACK_GRACE_SECONDS,
) -> Verdict:
    """判尸分类（纯函数）。见模块 docstring 的规则表。"""
    if view.status not in REAPABLE_STATUSES:
        return Verdict("skip", "terminal")
    if view.in_registry:
        return Verdict("skip", "in_registry")
    if view.mode == "remote":
        return Verdict("skip", "remote_unverifiable")

    budget_age = (
        view.max_time_minutes * 60 * BUDGET_AGE_MULTIPLIER + callback_grace_seconds
    )

    if view.mode == "process":
        if view.probe == "alive":
            return Verdict("skip", "process_alive")
        if view.probe == "dead":
            # pid 从未记录（None）且行年轻 = 启动窗口（spawn 前/回写前）：
            # 判死不成立。已记录过 pid 的死 = 可验证死亡，立即判死（MEDIUM-5）。
            if (
                view.instance_id is None
                and view.status in YOUNG_GRACE_STATUSES
                and view.age_seconds <= pending_grace_seconds
            ):
                return Verdict("skip", "young_active")
            return Verdict("mark_failed", "process_gone")
        # pid 不可读/未记录
        if view.age_seconds > budget_age:
            return Verdict("mark_failed", "exceeded_budget_unverifiable")
        return Verdict("skip", "process_unverified")

    # docker
    if view.probe in _DOCKER_PRESENT_STATES:
        return Verdict("reattach", "container_present")
    if view.probe == "unavailable":
        if view.age_seconds > budget_age:
            return Verdict("mark_failed", "exceeded_budget_unverifiable")
        return Verdict("skip", "probe_unavailable")
    # probe == missing（容器确定不存在）：provisioning 期间容器可能尚未建完
    if (
        view.status in YOUNG_GRACE_STATUSES
        and view.age_seconds <= pending_grace_seconds
    ):
        return Verdict("skip", "young_active")
    return Verdict("mark_failed", "container_missing")


# ── 真实依赖（收集器 / 动作）───────────────────────────────────────────────


def _docker_client():
    """懒建 docker client（API 进程有 docker.sock；失败返回 None）。"""
    global _DOCKER
    if _DOCKER is not False:
        return _DOCKER
    try:
        import docker

        _DOCKER = docker.from_env(timeout=int(os.getenv("DOCKER_CLIENT_TIMEOUT", "30")))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Reaper] docker client 不可用: %s", exc)
        _DOCKER = False
    return _DOCKER


_DOCKER: Any = False

_ORCHESTRATOR: Any = None


def _orchestrator():
    global _ORCHESTRATOR
    if _ORCHESTRATOR is None:
        from backend.services.engine.training.local_docker_orchestrator import (
            LocalDockerOrchestrator,
        )

        _ORCHESTRATOR = LocalDockerOrchestrator()
    return _ORCHESTRATOR


def _probe_docker(run_id: str) -> tuple[str, str | None]:
    """返回 (state, container_id)；NotFound → missing，daemon 异常 → unavailable。"""
    client = _docker_client()
    if not client:
        return "unavailable", None
    try:
        import docker

        c = client.containers.get(_CONTAINER_NAME_FMT.format(run_id=run_id))
        c.reload()
        state = str((c.attrs.get("State") or {}).get("Status") or "unavailable")
        return state, c.id
    except Exception as exc:  # noqa: BLE001
        try:
            import docker

            if isinstance(exc, docker.errors.NotFound):
                return "missing", None
        except Exception:  # noqa: BLE001
            pass
        logger.warning("[Reaper] probe %s failed: %s", run_id, exc)
        return "unavailable", None


def _probe_process(instance_id: str | None) -> str:
    pid_raw = str(instance_id or "").strip()
    if not pid_raw.isdigit():
        return "dead"  # 未记录 pid = 进程模式从未成功启动
    try:
        os.kill(int(pid_raw), 0)
        return "alive"
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"
    except Exception:  # noqa: BLE001
        return "dead"


def _job_mode(payload: dict) -> str:
    node_id = str(payload.get("node_id") or "local")
    if node_id.startswith("autodl"):
        return "remote"
    try:
        from backend.shared.training_runtime import resolve_training_executor

        if resolve_training_executor().get("executor") == "process":
            return "process"
    except Exception:  # noqa: BLE001
        pass
    return "docker"


def _max_time_minutes(payload: dict) -> int:
    try:
        return max(10, int(payload.get("max_time_minutes") or 120))
    except (TypeError, ValueError):
        return 120


async def _collect_job_views() -> list[tuple[JobView, dict]]:
    """默认收集器：DB 非终态行 → JobView（含探针/在管判定）+ payload。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session
    from backend.services.engine.training.orchestrator_base import REGISTRY

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT id, tenant_id, user_id, status, instance_id, "
                        "request_payload, created_at, updated_at "
                        "FROM admin_training_jobs "
                        "WHERE status IN ('pending','provisioning','running','waiting_callback') "
                        "ORDER BY created_at ASC"
                    )
                )
            )
            .mappings()
            .all()
        )

    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)  # 列存 naive UTC
    views: list[tuple[JobView, dict]] = []
    for row in rows:
        payload = (
            row["request_payload"] if isinstance(row["request_payload"], dict) else {}
        )
        mode = _job_mode(payload)
        run_id = str(row["id"])
        if mode == "docker":
            # docker SDK 是同步阻塞 IO：不能直接卡在事件循环里（MEDIUM-7）
            probe, _cid = await asyncio.to_thread(_probe_docker, run_id)
        elif mode == "process":
            probe = _probe_process(row["instance_id"])
        else:
            probe = "unavailable"
        ref = row["updated_at"] or row["created_at"] or now_naive
        age = max(0.0, (now_naive - ref).total_seconds())
        views.append(
            (
                JobView(
                    run_id=run_id,
                    status=str(row["status"] or ""),
                    tenant_id=str(row["tenant_id"] or "default"),
                    user_id=str(row["user_id"] or "unknown"),
                    instance_id=str(row["instance_id"]) if row["instance_id"] else None,
                    age_seconds=age,
                    max_time_minutes=_max_time_minutes(payload),
                    mode=mode,
                    probe=probe,
                    in_registry=REGISTRY.is_active(run_id),
                ),
                payload,
            )
        )
    return views


async def _reattach_job(
    view: JobView, payload: dict, *, supervise: bool = True
) -> bool:
    """重挂孤儿容器（docker 模式）；容器又消失等竞态 → False。

    supervise=False（CLI 进程）仅探测存活，不建立监管——监管 task 挂在
    本进程的事件循环上，CLI 退出即消亡，建立监管是假象（复审 MEDIUM-6）。
    """
    client = _docker_client()
    if not client:
        return False
    try:
        import docker

        # docker SDK 同步阻塞 IO → to_thread（MEDIUM-7）
        c = await asyncio.to_thread(
            client.containers.get, _CONTAINER_NAME_FMT.format(run_id=view.run_id)
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("[Reaper] reattach %s: 容器已不在（%s）", view.run_id, exc)
        return False
    try:
        await _orchestrator().reattach_training_job(
            run_id=view.run_id,
            payload=payload,
            container_id=c.id,
            tenant_id=view.tenant_id,
            user_id=view.user_id,
            supervise=supervise,
        )
        logger.warning(
            "[Reaper] reattached orphan container: run_id=%s container=%s supervise=%s",
            view.run_id,
            c.id[:12],
            supervise,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("[Reaper] reattach %s failed: %s", view.run_id, exc)
        return False


async def _mark_orphaned(view: JobView, verdict: Verdict) -> bool:
    """判死回收：状态仍非终态才落 failed；随后释放单飞锁（CAS）。"""
    from backend.services.api.routers.admin.db import TrainingJobRecord
    from backend.shared.database_manager_v2 import get_session

    note = (
        f"{_RUN_JOB_DONE_MARK} 判死回收（reason={verdict.reason}）: "
        f"状态={view.status} 探针={view.probe} mode={view.mode} "
        f"age={int(view.age_seconds)}s 预算={view.max_time_minutes}min；默认不自动重投\n"
    )
    updated = False
    async with get_session() as session:
        rec = await session.get(TrainingJobRecord, view.run_id)
        if rec is None:
            logger.info("[Reaper] %s 行已不存在，跳过标记", view.run_id)
        elif str(rec.status or "") not in REAPABLE_STATUSES:
            logger.info(
                "[Reaper] %s 状态已变为 %s（竞态），跳过标记", view.run_id, rec.status
            )
        else:
            rec.status = "failed"
            rec.progress = 100
            rec.logs = (rec.logs or "") + note
            await session.commit()
            updated = True

    # 释放单飞锁（CAS：仅当持有者仍是本 run 才生效）
    try:
        from backend.shared.redis_sentinel_client import get_redis_sentinel_client
        from backend.shared.training_singleflight import release

        # 同步 redis 客户端阻塞 IO → to_thread（MEDIUM-7）
        await asyncio.to_thread(release, get_redis_sentinel_client(), view.run_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[Reaper] release singleflight for %s failed: %s", view.run_id, exc
        )

    if updated:
        try:
            from backend.services.engine.training.training_log_stream import (
                TrainingRunLogStream,
            )

            TrainingRunLogStream().append_log(
                run_id=view.run_id,
                tenant_id=view.tenant_id,
                user_id=view.user_id,
                line=note.strip(),
                status="failed",
                progress=100,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Reaper] append log for %s failed: %s", view.run_id, exc)

        # 滚动重训台账回流（P1 · §4.2，验收 ③）：kill -9 重启后判死的 run
        # 不能把台账行留在 dispatched 永装「在跑」。只命中滚动派发的 run
        # （无台账行时无害 no-op），best-effort 不阻塞回收循环。
        try:
            from backend.shared.rolling_campaigns import mark_outcome_by_run_safe

            await mark_outcome_by_run_safe(
                view.run_id, status="failed", reason=f"orphaned:{verdict.reason}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[Reaper] rolling campaign outcome for %s failed: %s", view.run_id, exc
            )
        logger.warning(
            "[Reaper] orphaned job marked failed: run_id=%s reason=%s",
            view.run_id,
            verdict.reason,
        )
    return updated


# ── 编排壳 ──────────────────────────────────────────────────────────────────


async def reconcile_training_jobs(
    *,
    apply: bool = True,
    job_views_provider: Callable[[], Awaitable[list[tuple[JobView, dict]]]]
    | None = None,
    reattach_fn: Callable[[JobView, dict], Awaitable[bool]] | None = None,
    mark_failed_fn: Callable[[JobView, Verdict], Awaitable[bool]] | None = None,
    write_heartbeat: bool = True,
    supervise_reattach: bool = True,
) -> dict:
    """判尸一轮：分类 → （apply 时）重挂/判死。dry-run 只输出计划。

    supervise_reattach=False（CLI 进程）：重挂只探测存活、不建立监管——
    监管 task 依附本进程事件循环，CLI 退出即消亡（复审 MEDIUM-6/LOW-10）。

    Returns
    -------
    dict : {scanned, dry_run, reattached, failed, planned_reattach, planned_failed,
            skipped: {reason: count}, errors: [...]}
    """
    provider = job_views_provider or _collect_job_views
    do_reattach = reattach_fn or functools.partial(
        _reattach_job, supervise=supervise_reattach
    )
    do_fail = mark_failed_fn or _mark_orphaned

    summary: dict[str, Any] = {
        "scanned": 0,
        "dry_run": not apply,
        "reattached": 0,
        "failed": 0,
        "planned_reattach": [],
        "planned_failed": [],
        "skipped": {},
        "errors": [],
    }

    try:
        views = await provider()
    except Exception as exc:  # noqa: BLE001
        logger.error("[Reaper] 收集作业失败: %s", exc)
        summary["errors"].append(f"collect: {exc}")
        return summary

    for view, payload in views:
        summary["scanned"] += 1
        verdict = classify_job(view)
        if verdict.action == "skip":
            summary["skipped"][verdict.reason] = (
                summary["skipped"].get(verdict.reason, 0) + 1
            )
            continue
        if verdict.action == "reattach":
            summary["planned_reattach"].append(view.run_id)
            if apply:
                try:
                    if await do_reattach(view, payload):
                        summary["reattached"] += 1
                except Exception as exc:  # noqa: BLE001
                    summary["errors"].append(f"reattach {view.run_id}: {exc}")
            continue
        # mark_failed
        summary["planned_failed"].append(view.run_id)
        if apply:
            try:
                if await do_fail(view, verdict):
                    summary["failed"] += 1
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append(f"mark_failed {view.run_id}: {exc}")

    if write_heartbeat:
        try:
            from backend.shared.scheduler_registry import heartbeat as _sched_heartbeat

            _sched_heartbeat("training_reaper")
        except Exception:  # noqa: BLE001
            pass
    if apply and (summary["reattached"] or summary["failed"]):
        logger.warning(
            "[Reaper] 本轮：扫描=%d 重挂=%d 判死=%d 跳过=%s",
            summary["scanned"],
            summary["reattached"],
            summary["failed"],
            summary["skipped"],
        )
    return summary


# ── 进程内常驻循环（API 进程 lifespan 启停）────────────────────────────────


def reaper_enabled() -> bool:
    try:
        from backend.shared.scheduler_registry import JOBS_BY_KEY, switch_enabled

        spec = JOBS_BY_KEY.get("training_reaper")
        if spec is not None:
            return switch_enabled(spec)
    except Exception:  # noqa: BLE001
        pass
    raw = str(os.getenv("TRAINING_JOB_REAPER_ENABLED", "")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def reaper_interval_seconds() -> int:
    try:
        return max(60, int(os.getenv("TRAINING_JOB_REAPER_INTERVAL_SECONDS", "3600")))
    except (TypeError, ValueError):
        return 3600


_REAPER_TASK: asyncio.Task | None = None


async def _reaper_loop(interval_seconds: int) -> None:
    while True:
        try:
            await reconcile_training_jobs(apply=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("[Reaper] sweep failed: %s", exc, exc_info=True)
        await asyncio.sleep(interval_seconds)


def start_training_job_reaper() -> bool:
    """启动常驻回收循环（启动期立即扫一轮，随后按周期）。幂等。"""
    global _REAPER_TASK
    if not reaper_enabled():
        logger.info("[Reaper] 已禁用（TRAINING_JOB_REAPER_ENABLED=false），不启动")
        return False
    if _REAPER_TASK is not None and not _REAPER_TASK.done():
        return False
    _REAPER_TASK = asyncio.create_task(_reaper_loop(reaper_interval_seconds()))
    logger.info("[Reaper] started (interval=%ds)", reaper_interval_seconds())
    return True


async def stop_training_job_reaper() -> None:
    global _REAPER_TASK
    task = _REAPER_TASK
    _REAPER_TASK = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    """运维 CLI：--dry-run 只打印计划；--apply 执行一轮。"""
    parser = argparse.ArgumentParser(description="训练僵尸作业判尸回收（P0-3）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只输出判定计划（默认）")
    mode.add_argument("--apply", action="store_true", help="实际重挂/判死")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    apply = bool(args.apply)
    # CLI：不写心跳（C07 体检应反映 API 进程内循环）、重挂不建监管（CLI 退出即消亡）
    summary = asyncio.run(
        reconcile_training_jobs(
            apply=apply, write_heartbeat=False, supervise_reattach=False
        )
    )
    print(f"[Reaper] dry_run={summary['dry_run']} scanned={summary['scanned']}")
    print(f"  计划重挂: {summary['planned_reattach']}")
    if apply and summary["planned_reattach"]:
        print("  注意: CLI 仅探测存活、不建立监管；监管由 API 进程内回收循环恢复")
    print(f"  计划判死: {summary['planned_failed']}")
    print(f"  跳过: {summary['skipped']}")
    if summary["errors"]:
        print(f"  错误: {summary['errors']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
