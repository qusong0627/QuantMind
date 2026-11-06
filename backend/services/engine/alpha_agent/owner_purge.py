"""Owner-scoped hard purge of all factor-mining data.

The purge is admitted as a durable ``alpha_factor_jobs`` row of kind ``purge``
so it inherits the existing lease, outbox, cancellation and reconciliation
machinery. The purge job itself, its lease, request key and outbox row survive
as the audit record of the operation.

Preview and execution share one snapshot fingerprint. The fingerprint covers
the owner's task/factor/document/job identities, per-table counts and the
owner-scoped artifact byte total. Execution takes the execution-pool locks
first, recomputes the fingerprint in the same transaction and refuses to
delete when anything changed since preview.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import text

from backend.shared.database_manager_v2 import get_session
from backend.shared.owner_path import owner_segment, user_scoped_dir
from backend.shared.owner_scope import OwnerScope
from backend.shared.utc_datetime import to_utc_iso, utc_now

logger = logging.getLogger(__name__)

_ACTIVE_TASK = ("pending", "queued", "running", "stopping")
_ACTIVE_JOB = ("queued", "running", "stopping")
_LEASE_POOLS = ("alpha_mining", "alpha_factor_jobs", "alpha_research_llm")
_MAX_ENTRIES = 500_000


class OwnerPurgeConflict(ValueError):
    """The owner snapshot changed or the purge cannot run safely."""


class OwnerPurgeBlocked(OwnerPurgeConflict):
    """Active work still occupies the owner's mining/job/research pools."""


class OwnerPurgeCancelled(RuntimeError):
    """The purge was stopped before the next irreversible phase."""


#: label, table, extra predicate. Owner predicates are added by the builder.
_COUNT_TABLES: tuple[tuple[str, str, str], ...] = (
    ("tasks", "alpha_mining_tasks", ""),
    ("mining_materialization_outbox", "alpha_mining_materialization_outbox", ""),
    ("task_specs", "alpha_mining_task_specs", ""),
    ("task_results", "alpha_mining_task_results", ""),
    ("mining_request_keys", "alpha_mining_request_keys", ""),
    ("factors", "rd_agent_factors", ""),
    ("factor_revisions", "rd_agent_factor_revisions", ""),
    ("factor_pool", "rd_agent_factor_pool", ""),
    ("factor_edges", "rd_agent_factor_edges", ""),
    (
        "factor_jobs",
        "alpha_factor_jobs",
        "AND kind <> 'purge' AND job_id <> :purge_job",
    ),
    (
        "factor_job_outbox",
        "alpha_factor_job_outbox",
        "AND job_id <> :purge_job",
    ),
    (
        "factor_job_request_keys",
        "alpha_factor_job_request_keys",
        "AND job_id <> :purge_job",
    ),
    ("factor_validation_attempts", "alpha_factor_validation_attempts", ""),
    ("factor_validation_latest", "alpha_factor_validation_latest", ""),
    ("factor_materializations", "alpha_factor_materializations", ""),
    ("factor_recovery_sources", "alpha_factor_recovery_sources", ""),
    ("factor_visibility_events", "alpha_factor_visibility_events", ""),
    ("factor_lifecycle_events", "rd_agent_factor_lifecycle_events", ""),
    ("factor_generations", "alpha_user_factor_generations", ""),
    ("factor_generation_features", "alpha_user_factor_generation_features", ""),
    ("factor_dataset_active", "alpha_user_factor_dataset_active", ""),
    ("factor_combos", "alpha_factor_combos", ""),
    ("factor_combo_members", "alpha_factor_combo_members", ""),
    ("artifact_cleanups", "alpha_task_artifact_cleanups", ""),
    ("docs", "rd_agent_docs", ""),
    ("papers", "user_papers", ""),
    ("direction_batches", "alpha_direction_batches", ""),
    ("research_requests", "alpha_research_requests", ""),
    ("research_llm_calls", "alpha_research_llm_calls", ""),
    ("variant_contexts", "alpha_variant_contexts", ""),
    ("variant_observations", "alpha_variant_observations", ""),
    ("variant_states", "alpha_variant_states", ""),
    ("variant_reset_events", "alpha_variant_reset_events", ""),
    # 节点分配以 (pool,task_id,tenant_id,user_id) 外键引用 resource_task_leases，
    # 必须先删分配再删租约，否则清理执行会撞 FK 约束。
    ("node_assignments", "alpha_mining_node_assignments", ""),
    (
        "leases",
        "resource_task_leases",
        "AND pool IN ('alpha_mining','alpha_factor_jobs','alpha_research_llm') "
        "AND NOT (pool='alpha_factor_jobs' AND task_id=:purge_job)",
    ),
)

#: label, table, id column, extra predicate. Only identities that can change
#: without changing a count need a digest.
_DIGEST_TABLES: tuple[tuple[str, str, str, str], ...] = (
    ("tasks", "alpha_mining_tasks", "task_id", ""),
    ("factors", "rd_agent_factors", "factor_id", ""),
    (
        "factor_jobs",
        "alpha_factor_jobs",
        "job_id",
        "AND kind <> 'purge' AND job_id <> :purge_job",
    ),
    ("docs", "rd_agent_docs", "doc_id", ""),
    ("papers", "user_papers", "paper_id", ""),
    ("generations", "alpha_user_factor_generations", "generation_id", ""),
    ("research_requests", "alpha_research_requests", "request_id", ""),
    ("direction_batches", "alpha_direction_batches", "batch_id", ""),
    ("variant_contexts", "alpha_variant_contexts", "context_id", ""),
)

#: Delete order reverses every foreign-key edge. ``rd_agent_docs`` must go
#: before jobs (docs reference parse/organize jobs), while
#: ``alpha_mining_tasks.source_doc_id`` is nulled first to break the cycle
#: tasks -> docs -> jobs -> tasks.
_DELETE_TABLES: tuple[tuple[str, str, str], ...] = (
    ("mining_materialization_outbox", "alpha_mining_materialization_outbox", ""),
    ("artifact_cleanups", "alpha_task_artifact_cleanups", ""),
    ("factor_validation_attempts", "alpha_factor_validation_attempts", ""),
    ("factor_validation_latest", "alpha_factor_validation_latest", ""),
    ("factor_recovery_sources", "alpha_factor_recovery_sources", ""),
    ("factor_materializations", "alpha_factor_materializations", ""),
    ("factor_generation_features", "alpha_user_factor_generation_features", ""),
    ("factor_dataset_active", "alpha_user_factor_dataset_active", ""),
    ("factor_generations", "alpha_user_factor_generations", ""),
    ("factor_lifecycle_events", "rd_agent_factor_lifecycle_events", ""),
    ("factor_visibility_events", "alpha_factor_visibility_events", ""),
    ("factor_edges", "rd_agent_factor_edges", ""),
    ("factor_combo_members", "alpha_factor_combo_members", ""),
    ("factor_combos", "alpha_factor_combos", ""),
    ("factor_pool", "rd_agent_factor_pool", ""),
    ("factor_revisions", "rd_agent_factor_revisions", ""),
    ("factors", "rd_agent_factors", ""),
    (
        "factor_job_outbox",
        "alpha_factor_job_outbox",
        "AND job_id <> :purge_job",
    ),
    (
        "factor_job_request_keys",
        "alpha_factor_job_request_keys",
        "AND job_id <> :purge_job",
    ),
    ("docs", "rd_agent_docs", ""),
    ("papers", "user_papers", ""),
    ("factor_jobs", "alpha_factor_jobs", "AND job_id <> :purge_job"),
    ("mining_request_keys", "alpha_mining_request_keys", ""),
    ("variant_observations", "alpha_variant_observations", ""),
    ("variant_states", "alpha_variant_states", ""),
    ("variant_reset_events", "alpha_variant_reset_events", ""),
    ("task_results", "alpha_mining_task_results", ""),
    ("task_specs", "alpha_mining_task_specs", ""),
    ("tasks", "alpha_mining_tasks", ""),
    ("direction_batches", "alpha_direction_batches", ""),
    ("research_llm_calls", "alpha_research_llm_calls", ""),
    ("research_requests", "alpha_research_requests", ""),
    ("variant_contexts", "alpha_variant_contexts", ""),
    # alpha_mining_node_assignments 以 (pool,task_id,tenant_id,user_id) 外键
    # 引用 resource_task_leases，必须先删分配再删租约，否则撞 FK 约束。
    ("node_assignments", "alpha_mining_node_assignments", ""),
    (
        "leases",
        "resource_task_leases",
        "AND pool IN ('alpha_mining','alpha_factor_jobs','alpha_research_llm') "
        "AND NOT (pool='alpha_factor_jobs' AND task_id=:purge_job)",
    ),
)

_OWNER = "tenant_id=:tenant_id AND user_id=:user_id"


def _params(owner: OwnerScope, purge_job: str = "") -> dict[str, Any]:
    return {**owner.sql_params(), "purge_job": purge_job}


def _counts_sql():
    selects = [
        f"SELECT '{label}' AS k, count(*)::bigint AS n FROM {table} "
        f"WHERE {_OWNER}{(' ' + extra) if extra else ''}"
        for label, table, extra in _COUNT_TABLES
    ]
    return text(" UNION ALL ".join(selects))


def _digests_sql():
    selects = [
        f"SELECT '{label}' AS k, "
        f"md5(coalesce(string_agg({column}, ',' ORDER BY {column}), '')) AS d "
        f"FROM {table} WHERE {_OWNER}{(' ' + extra) if extra else ''}"
        for label, table, column, extra in _DIGEST_TABLES
    ]
    return text(" UNION ALL ".join(selects))


def _blockers_sql():
    return text(
        "SELECT 'mining_tasks' AS k, count(*)::bigint AS n FROM alpha_mining_tasks "
        f"WHERE {_OWNER} AND status IN ('pending','queued','running','stopping') "
        "UNION ALL SELECT 'factor_jobs', count(*)::bigint FROM alpha_factor_jobs "
        f"WHERE {_OWNER} AND kind <> 'purge' AND status IN ('queued','running','stopping') "
        "UNION ALL SELECT 'purge_jobs', count(*)::bigint FROM alpha_factor_jobs "
        f"WHERE {_OWNER} AND kind='purge' AND job_id <> :purge_job "
        "AND status IN ('queued','running','stopping') "
        "UNION ALL SELECT 'research_requests', count(*)::bigint FROM alpha_research_requests "
        f"WHERE {_OWNER} AND status IN ('queued','running') "
        "UNION ALL SELECT 'leases', count(*)::bigint FROM resource_task_leases "
        f"WHERE {_OWNER} AND pool IN ('alpha_mining','alpha_factor_jobs','alpha_research_llm') "
        "AND state IN ('queued','reserved','running','suspect') "
        "AND NOT (pool='alpha_factor_jobs' AND task_id=:purge_job)"
    )


async def _snapshot_in_session(
    session, owner: OwnerScope, purge_job: str = ""
) -> tuple[dict[str, int], dict[str, str]]:
    params = _params(owner, purge_job)
    count_rows = (await session.execute(_counts_sql(), params)).mappings().all()
    counts = {row["k"]: int(row["n"]) for row in count_rows}
    digest_rows = (await session.execute(_digests_sql(), params)).mappings().all()
    digests = {row["k"]: str(row["d"] or "") for row in digest_rows}
    return counts, digests


async def _snapshot(
    owner: OwnerScope, purge_job: str = ""
) -> tuple[dict[str, int], dict[str, str]]:
    async with get_session() as session:
        await session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        )
        return await _snapshot_in_session(session, owner, purge_job)


async def _active_blockers(
    owner: OwnerScope, purge_job: str = ""
) -> dict[str, int]:
    async with get_session() as session:
        rows = (
            (await session.execute(_blockers_sql(), _params(owner, purge_job)))
            .mappings()
            .all()
        )
    return {row["k"]: int(row["n"]) for row in rows if int(row["n"]) > 0}


def _fingerprint(
    owner: OwnerScope,
    counts: dict[str, int],
    digests: dict[str, str],
    storage_bytes: int,
) -> str:
    payload = {
        "tenant_id": owner.tenant_id,
        "user_id": owner.user_id,
        "counts": counts,
        "digests": digests,
        "storage_bytes": int(storage_bytes),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _owner_roots(owner: OwnerScope) -> dict[str, tuple[Path, Path]]:
    """Return ``label -> (allowed root, owner target)`` for artifact deletion."""
    from backend.services.engine.alpha_agent.factor_pool_panels import panel_root
    from backend.services.engine.alpha_agent.launcher import get_launcher
    from backend.shared.feature_source import quantcustom_root

    log_root = Path(get_launcher()._log_dir)
    artifacts_root = Path(
        os.getenv("PIPELINE_ARTIFACTS_DIR", "/data/pipeline_artifacts")
    )
    custom_root = quantcustom_root() / "users"
    panel_root_path = panel_root()
    return {
        "task_workspaces": (
            log_root,
            user_scoped_dir(log_root, owner.tenant_id, owner.user_id),
        ),
        "docs": (
            artifacts_root,
            user_scoped_dir(artifacts_root, owner.tenant_id, owner.user_id)
            / "alpha_research",
        ),
        "exports": (
            artifacts_root,
            user_scoped_dir(artifacts_root, owner.tenant_id, owner.user_id)
            / "exports",
        ),
        "factor_generations": (
            custom_root,
            user_scoped_dir(custom_root, owner.tenant_id, owner.user_id)
            / "factor_generations",
        ),
        "factor_exports": (
            custom_root,
            user_scoped_dir(custom_root, owner.tenant_id, owner.user_id)
            / "exports",
        ),
        "pool_panels": (
            panel_root_path,
            user_scoped_dir(panel_root_path, owner.tenant_id, owner.user_id),
        ),
    }


def _safe_owner_path(root: Path, target: Path) -> Path | None:
    root = Path(root).absolute()
    target = Path(target).absolute()
    if root.is_symlink():
        raise OwnerPurgeConflict("产物根目录是符号链接，拒绝清理")
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise OwnerPurgeConflict("产物路径越出属主目录，拒绝清理") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise OwnerPurgeConflict("产物路径包含符号链接，拒绝清理")
        if not current.exists():
            return None
        if not current.is_dir():
            raise OwnerPurgeConflict("产物路径不是目录，拒绝清理")
    return current


def _inventory(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"exists": False, "bytes": 0, "entries": 0}
    total = 0
    entries = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as listing:
                for entry in listing:
                    entries += 1
                    if entries > _MAX_ENTRIES:
                        raise OwnerPurgeConflict("产物数量超出清理上限")
                    if entry.is_symlink():
                        # 软链只跳过，不跟随也不计入体积：因子挖掘会在任务工作区
                        # 生成 git_ignore_folder/**_source_data/daily_pv.parquet
                        # 指向共享 panel 缓存（/data/mining_cache/panels，按内容
                        # 哈希去重），它既不属于属主也不应随单任务清理删除。
                        # 与 backend/shared/storage_quota.py 的盘点口径保持一致。
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                    else:
                        raise OwnerPurgeConflict("产物目录包含非普通文件，拒绝清理")
        except FileNotFoundError:
            continue
    return {"exists": True, "bytes": total, "entries": entries}


def _storage_inventory(owner: OwnerScope) -> dict[str, Any]:
    items: dict[str, Any] = {}
    total = 0
    for label, (root, target) in _owner_roots(owner).items():
        path = _safe_owner_path(root, target)
        inventory = _inventory(path)
        items[label] = inventory
        total += int(inventory["bytes"])
    return {"items": items, "total_bytes": total}


def _remove_storage(owner: OwnerScope, expected: dict[str, Any]) -> int:
    removed = 0
    for label, (root, target) in _owner_roots(owner).items():
        path = _safe_owner_path(root, target)
        if path is None:
            continue
        current = _inventory(path)
        wanted = expected["items"].get(label) or {
            "exists": False,
            "bytes": 0,
            "entries": 0,
        }
        if current != wanted:
            raise OwnerPurgeConflict("产物文件在预览后发生变化，请重新预览")
        if path.exists():
            shutil.rmtree(path)
            removed += int(current["bytes"])
    return removed


def _quota_prefixes(owner: OwnerScope) -> list[str]:
    tenant = owner_segment(owner.tenant_id)
    user = owner_segment(owner.user_id)
    return [
        f"pipeline_artifacts/{tenant}/{user}/alpha_research/%",
        f"pipeline_artifacts/{tenant}/{user}/exports/%",
        f"factor_pool_panels/{tenant}/{user}/%",
        f"user_factor_datasets/{tenant}/{user}/%",
    ]


async def _cleanup_quota(owner: OwnerScope) -> dict[str, int]:
    prefixes = _quota_prefixes(owner)
    async with get_session() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT count(*)::bigint AS objects, "
                        "coalesce(sum(size_bytes),0)::bigint AS amount "
                        "FROM storage_quota_objects WHERE tenant_id=:tenant_id "
                        "AND user_id=:user_id "
                        "AND resource_key LIKE ANY(CAST(:prefixes AS TEXT[]))"
                    ),
                    {**owner.sql_params(), "prefixes": prefixes},
                )
            )
            .mappings()
            .one()
        )
        amount = int(row["amount"])
        objects = int(row["objects"])
        if amount or objects:
            await session.execute(
                text(
                    "DELETE FROM storage_quota_objects WHERE tenant_id=:tenant_id "
                    "AND user_id=:user_id "
                    "AND resource_key LIKE ANY(CAST(:prefixes AS TEXT[]))"
                ),
                {**owner.sql_params(), "prefixes": prefixes},
            )
        if amount:
            await session.execute(
                text(
                    "UPDATE storage_quota_accounts SET "
                    "used_bytes=GREATEST(0, used_bytes-:amount), updated_at=now() "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id"
                ),
                {**owner.sql_params(), "amount": amount},
            )
    return {"objects": objects, "bytes": amount}


_DISABLE_TRIGGERS = text(
    """
DO $purge$
BEGIN
    IF to_regclass('rd_agent_factor_revisions') IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM pg_trigger
           WHERE tgname='rd_agent_factor_revision_immutable'
             AND tgrelid=to_regclass('rd_agent_factor_revisions')
       ) THEN
        EXECUTE 'ALTER TABLE rd_agent_factor_revisions DISABLE TRIGGER rd_agent_factor_revision_immutable';
    END IF;
    IF to_regclass('rd_agent_factor_lifecycle_events') IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM pg_trigger
           WHERE tgname='rd_agent_factor_lifecycle_events_immutable'
             AND tgrelid=to_regclass('rd_agent_factor_lifecycle_events')
       ) THEN
        EXECUTE 'ALTER TABLE rd_agent_factor_lifecycle_events DISABLE TRIGGER rd_agent_factor_lifecycle_events_immutable';
    END IF;
END
$purge$;
"""
)

_ENABLE_TRIGGERS = text(
    """
DO $purge$
BEGIN
    IF to_regclass('rd_agent_factor_revisions') IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM pg_trigger
           WHERE tgname='rd_agent_factor_revision_immutable'
             AND tgrelid=to_regclass('rd_agent_factor_revisions')
       ) THEN
        EXECUTE 'ALTER TABLE rd_agent_factor_revisions ENABLE TRIGGER rd_agent_factor_revision_immutable';
    END IF;
    IF to_regclass('rd_agent_factor_lifecycle_events') IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM pg_trigger
           WHERE tgname='rd_agent_factor_lifecycle_events_immutable'
             AND tgrelid=to_regclass('rd_agent_factor_lifecycle_events')
       ) THEN
        EXECUTE 'ALTER TABLE rd_agent_factor_lifecycle_events ENABLE TRIGGER rd_agent_factor_lifecycle_events_immutable';
    END IF;
END
$purge$;
"""
)


async def _delete_database(
    owner: OwnerScope,
    purge_job: str,
    preview_token: str,
    storage_bytes: int,
) -> dict[str, int]:
    params = _params(owner, purge_job)
    async with get_session() as session:
        await session.execute(text("SET LOCAL statement_timeout='300s'"))
        await session.execute(text("SET LOCAL lock_timeout='20s'"))
        # All mining/job/research admission paths take this pool row lock, so
        # no new work can be admitted between the fingerprint check and delete.
        await session.execute(
            text(
                "SELECT pool FROM resource_lease_pools "
                "WHERE pool = ANY(CAST(:pools AS TEXT[])) FOR UPDATE"
            ),
            {"pools": list(_LEASE_POOLS)},
        )
        counts, digests = await _snapshot_in_session(session, owner, purge_job)
        if _fingerprint(owner, counts, digests, storage_bytes) != preview_token:
            raise OwnerPurgeConflict("数据在预览后发生变化，请重新预览")
        await session.execute(
            text(f"UPDATE alpha_mining_tasks SET source_doc_id=NULL WHERE {_OWNER}"),
            params,
        )
        await session.execute(_DISABLE_TRIGGERS)
        deleted: dict[str, int] = {}
        for label, table, extra in _DELETE_TABLES:
            statement = text(
                f"DELETE FROM {table} WHERE {_OWNER}{(' ' + extra) if extra else ''}"
            )
            result = await session.execute(statement, params)
            deleted[label] = max(0, int(result.rowcount or 0))
        await session.execute(_ENABLE_TRIGGERS)
    return deleted


def _reconcile_budgets() -> None:
    from backend.services.engine.alpha_agent.factor_job_budget import (
        recover_budget as recover_factor_jobs,
    )
    from backend.services.engine.alpha_agent.research_requests import (
        reconcile as reconcile_research,
    )
    from backend.services.engine.alpha_agent.resource_budget import (
        recover_budget as recover_mining,
    )

    recover_mining()
    recover_factor_jobs()
    reconcile_research()


# --- single-task scope -------------------------------------------------------
# A task purge removes one mining task and everything derived exclusively from
# it (specs, results, factors, revisions, validations, still-building
# materializations, referencing terminal jobs and its workspace directory).
# Shared or named artifacts fail closed and are never touched: combos,
# published generations, active work, and cross-task memory (docs, direction
# batches, research requests, variant state).

_TASK_TABLES: tuple[tuple[str, str], ...] = (
    # request_keys references specs, specs/results reference tasks.
    ("mining_request_keys", "alpha_mining_request_keys"),
    ("task_specs", "alpha_mining_task_specs"),
    ("task_results", "alpha_mining_task_results"),
    ("artifact_cleanups", "alpha_task_artifact_cleanups"),
    # tasks last: everything above references it.
    ("tasks", "alpha_mining_tasks"),
)

_TASK_FACTOR_TABLES: tuple[tuple[str, str], ...] = (
    ("factor_revisions", "rd_agent_factor_revisions"),
    ("factor_pool", "rd_agent_factor_pool"),
    ("factor_visibility_events", "alpha_factor_visibility_events"),
    ("factor_lifecycle_events", "rd_agent_factor_lifecycle_events"),
    ("factor_validation_attempts", "alpha_factor_validation_attempts"),
    ("factor_validation_latest", "alpha_factor_validation_latest"),
    ("factor_recovery_sources", "alpha_factor_recovery_sources"),
    ("factor_materializations", "alpha_factor_materializations"),
    # factors last: every row above references it.
    ("factors", "rd_agent_factors"),
)

_TASK_GEN_TABLES: tuple[tuple[str, str], ...] = (
    ("factor_generation_features", "alpha_user_factor_generation_features"),
    ("factor_dataset_active", "alpha_user_factor_dataset_active"),
    ("factor_generations", "alpha_user_factor_generations"),
)

#: Jobs derived exclusively from one task: owned by it or frozen against one
#: of its factors (single, batch list or batch items). Purge-kind jobs never
#: match: they carry no factor reference, and the running purge itself is
#: excluded by job id.
_TASK_JOB_PRED = (
    "parent_task_id = :task "
    "OR request_snapshot #>> '{request,factor_id}' = ANY(CAST(:factors AS TEXT[])) "
    "OR (request_snapshot #> '{request,factor_ids}') ?| CAST(:factors AS TEXT[]) "
    "OR EXISTS (SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof("
    "request_snapshot #> '{request,factor_items}') = 'array' "
    "THEN request_snapshot #> '{request,factor_items}' ELSE '[]'::jsonb END"
    ") AS item(value) WHERE item.value ->> 'factor_id' = ANY(CAST(:factors AS TEXT[])))"
)


def _scoped_params(
    owner: OwnerScope,
    task_id: str,
    factors: list[str],
    gens: list[str],
    job_ids: list[str],
    purge_job: str = "",
) -> dict[str, Any]:
    return {
        **owner.sql_params(),
        "task": task_id,
        "factors": list(factors),
        "gens": list(gens),
        "job_ids": list(job_ids),
        "purge_job": purge_job,
    }


async def _scoped_identities(
    session, owner: OwnerScope, task_id: str
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Resolve one task's exclusive factor/generation/job identities.

    Returns ``(factors, building_gens, published_gens, job_ids)`` where
    ``job_ids`` covers every non-purge job derived from the task.
    """
    task_id = owner_segment(task_id)
    exists = (
        await session.execute(
            text(
                "SELECT 1 FROM alpha_mining_tasks WHERE tenant_id=:tenant_id "
                "AND user_id=:user_id AND task_id=:task"
            ),
            {**owner.sql_params(), "task": task_id},
        )
    ).scalar_one_or_none()
    if exists is None:
        raise OwnerPurgeConflict("任务不存在或已被删除")
    factor_rows = (
        await session.execute(
            text(
                "SELECT factor_id FROM rd_agent_factors WHERE tenant_id=:tenant_id "
                "AND user_id=:user_id AND metadata_json->>'task_id'=:task"
            ),
            {**owner.sql_params(), "task": task_id},
        )
    ).scalars().all()
    factors = sorted({str(value) for value in factor_rows if value})
    building: list[str] = []
    published: list[str] = []
    if factors:
        gen_rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT m.generation_id, g.status FROM "
                    "alpha_factor_materializations m LEFT JOIN "
                    "alpha_user_factor_generations g ON g.tenant_id=m.tenant_id "
                    "AND g.user_id=m.user_id AND g.generation_id=m.generation_id "
                    "WHERE m.tenant_id=:tenant_id AND m.user_id=:user_id "
                    "AND m.factor_id=ANY(CAST(:factors AS TEXT[]))"
                ),
                {**owner.sql_params(), "factors": factors},
            )
        ).mappings().all()
        for row in gen_rows:
            gen = str(row["generation_id"] or "")
            if not gen:
                continue
            if (row["status"] or "building") == "published":
                published.append(gen)
            else:
                building.append(gen)
    job_rows = (
        await session.execute(
            text(
                "SELECT job_id FROM alpha_factor_jobs WHERE tenant_id=:tenant_id "
                "AND user_id=:user_id AND kind NOT IN ('purge','purge_task') "
                f"AND ({_TASK_JOB_PRED})"
            ),
            {**owner.sql_params(), "task": task_id, "factors": factors},
        )
    ).scalars().all()
    job_ids = sorted({str(value) for value in job_rows if value})
    return factors, sorted(set(building)), sorted(set(published)), job_ids


def _scoped_counts_sql() -> Any:
    selects = [
        f"SELECT '{label}' AS k, count(*)::bigint AS n FROM {table} "
        f"WHERE {_OWNER} AND task_id=:task"
        for label, table in _TASK_TABLES
    ]
    selects.append(
        "SELECT 'mining_materialization_outbox' AS k, count(*)::bigint AS n "
        "FROM alpha_mining_materialization_outbox "
        "WHERE tenant_id=:tenant_id AND user_id=:user_id AND task_id=:task"
    )
    selects += [
        f"SELECT '{label}' AS k, count(*)::bigint AS n FROM {table} "
        f"WHERE {_OWNER} AND factor_id=ANY(CAST(:factors AS TEXT[]))"
        for label, table in _TASK_FACTOR_TABLES
    ]
    selects.append(
        "SELECT 'factor_edges' AS k, count(*)::bigint AS n FROM rd_agent_factor_edges "
        f"WHERE {_OWNER} AND (src_factor_id=ANY(CAST(:factors AS TEXT[])) "
        "OR dst_factor_id=ANY(CAST(:factors AS TEXT[])))"
    )
    selects.append(
        "SELECT 'factor_generation_features' AS k, count(*)::bigint AS n "
        "FROM alpha_user_factor_generation_features "
        "WHERE tenant_id=:tenant_id AND user_id=:user_id "
        "AND (generation_id=ANY(CAST(:gens AS TEXT[])) OR source_task_id=:task)"
    )
    selects += [
        f"SELECT '{label}' AS k, count(*)::bigint AS n FROM {table} "
        f"WHERE {_OWNER} AND generation_id=ANY(CAST(:gens AS TEXT[]))"
        for label, table in _TASK_GEN_TABLES[1:]
    ]
    selects.append(
        "SELECT 'factor_jobs' AS k, count(*)::bigint AS n FROM alpha_factor_jobs "
        "WHERE tenant_id=:tenant_id AND user_id=:user_id "
        "AND job_id=ANY(CAST(:job_ids AS TEXT[])) AND job_id <> :purge_job"
    )
    selects.append(
        "SELECT 'factor_job_outbox' AS k, count(*)::bigint AS n "
        "FROM alpha_factor_job_outbox "
        "WHERE tenant_id=:tenant_id AND user_id=:user_id "
        "AND job_id=ANY(CAST(:job_ids AS TEXT[])) AND job_id <> :purge_job"
    )
    selects.append(
        "SELECT 'factor_job_request_keys' AS k, count(*)::bigint AS n "
        "FROM alpha_factor_job_request_keys "
        "WHERE tenant_id=:tenant_id AND user_id=:user_id "
        "AND job_id=ANY(CAST(:job_ids AS TEXT[])) AND job_id <> :purge_job"
    )
    selects.append(
        "SELECT 'node_assignments' AS k, count(*)::bigint AS n "
        "FROM alpha_mining_node_assignments "
        "WHERE tenant_id=:tenant_id AND user_id=:user_id "
        "AND ((pool='alpha_mining' AND task_id=:task) "
        "OR (pool='alpha_factor_jobs' AND task_id=ANY(CAST(:job_ids AS TEXT[]))))"
    )
    selects.append(
        "SELECT 'leases' AS k, count(*)::bigint AS n FROM resource_task_leases "
        "WHERE tenant_id=:tenant_id AND user_id=:user_id "
        "AND ((pool='alpha_mining' AND task_id=:task) "
        "OR (pool='alpha_factor_jobs' AND task_id=ANY(CAST(:job_ids AS TEXT[])))) "
        "AND NOT (pool='alpha_factor_jobs' AND task_id=:purge_job)"
    )
    return text(" UNION ALL ".join(selects))


def _scoped_digests_sql() -> Any:
    return text(
        " UNION ALL ".join(
            [
                f"SELECT '{label}' AS k, md5(coalesce(string_agg({column}, ',' ORDER BY {column}), '')) AS d "
                f"FROM {table} WHERE {_OWNER}{extra}"
                for label, table, column, extra in (
                    ("tasks", "alpha_mining_tasks", "task_id", " AND task_id=:task"),
                    (
                        "factors",
                        "rd_agent_factors",
                        "factor_id",
                        " AND factor_id=ANY(CAST(:factors AS TEXT[]))",
                    ),
                    (
                        "factor_jobs",
                        "alpha_factor_jobs",
                        "job_id",
                        " AND job_id=ANY(CAST(:job_ids AS TEXT[])) AND job_id <> :purge_job",
                    ),
                    (
                        "generations",
                        "alpha_user_factor_generations",
                        "generation_id",
                        " AND generation_id=ANY(CAST(:gens AS TEXT[]))",
                    ),
                )
            ]
        )
    )


def _scoped_blockers_sql() -> Any:
    return text(
        "SELECT 'mining_task' AS k, count(*)::bigint AS n FROM alpha_mining_tasks "
        f"WHERE {_OWNER} AND task_id=:task AND status IN ('pending','queued','running','stopping') "
        "UNION ALL SELECT 'factor_jobs', count(*)::bigint FROM alpha_factor_jobs "
        f"WHERE {_OWNER} AND status IN ('queued','running','stopping') AND job_id <> :purge_job AND ({_TASK_JOB_PRED} "
        "OR kind IN ('purge','purge_task')) "
        "UNION ALL SELECT 'combos', count(DISTINCT m.combo_id)::bigint "
        "FROM alpha_factor_combo_members m "
        "WHERE m.tenant_id=:tenant_id AND m.user_id=:user_id "
        "AND m.factor_id=ANY(CAST(:factors AS TEXT[])) "
        "UNION ALL SELECT 'published_generations', count(DISTINCT m.generation_id)::bigint "
        "FROM alpha_factor_materializations m JOIN alpha_user_factor_generations g "
        "ON g.tenant_id=m.tenant_id AND g.user_id=m.user_id AND g.generation_id=m.generation_id "
        "WHERE m.tenant_id=:tenant_id AND m.user_id=:user_id "
        "AND m.factor_id=ANY(CAST(:factors AS TEXT[])) AND g.status='published' "
        "UNION ALL SELECT 'leases', count(*)::bigint FROM resource_task_leases "
        f"WHERE {_OWNER} AND state IN ('queued','reserved','running','suspect') "
        "AND ((pool='alpha_mining' AND task_id=:task) "
        "OR (pool='alpha_factor_jobs' AND task_id=ANY(CAST(:job_ids AS TEXT[])))) "
        "AND NOT (pool='alpha_factor_jobs' AND task_id=:purge_job)"
    )


async def _scoped_snapshot_in_session(
    session, owner: OwnerScope, task_id: str, purge_job: str = ""
) -> tuple[dict[str, int], dict[str, str], dict[str, list[str]]]:
    factors, building, published, job_ids = await _scoped_identities(
        session, owner, task_id
    )
    params = _scoped_params(owner, task_id, factors, building, job_ids, purge_job)
    count_rows = (await session.execute(_scoped_counts_sql(), params)).mappings().all()
    counts = {row["k"]: int(row["n"]) for row in count_rows}
    digest_rows = (await session.execute(_scoped_digests_sql(), params)).mappings().all()
    digests = {row["k"]: str(row["d"] or "") for row in digest_rows}
    return counts, digests, {"factors": factors, "gens": building, "published": published}


async def _scoped_blockers(owner: OwnerScope, task_id: str, purge_job: str = "") -> dict[str, int]:
    task_id = owner_segment(task_id)
    async with get_session() as session:
        factors, _building, _published, job_ids = await _scoped_identities(
            session, owner, task_id
        )
        params = _scoped_params(owner, task_id, factors, [], job_ids, purge_job)
        rows = (
            (await session.execute(_scoped_blockers_sql(), params)).mappings().all()
        )
    return {row["k"]: int(row["n"]) for row in rows if int(row["n"]) > 0}


def _scoped_fingerprint(
    owner: OwnerScope,
    task_id: str,
    counts: dict[str, int],
    digests: dict[str, str],
    storage_bytes: int,
) -> str:
    payload = {
        "tenant_id": owner.tenant_id,
        "user_id": owner.user_id,
        "scope": {"task_id": task_id},
        "counts": counts,
        "digests": digests,
        "storage_bytes": int(storage_bytes),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _scoped_storage_inventory(
    owner: OwnerScope, task_id: str, generations: list[str]
) -> dict[str, Any]:
    """Inventory one task workspace plus its building generation directories."""
    from backend.services.engine.alpha_agent.launcher import get_launcher
    from backend.shared.feature_source import quantcustom_root

    task_id = owner_segment(task_id)
    log_root = Path(get_launcher()._log_dir)
    custom_root = quantcustom_root() / "users"
    roots: dict[str, tuple[Path, Path]] = {
        "task_workspace": (
            log_root,
            user_scoped_dir(log_root, owner.tenant_id, owner.user_id)
            / task_id,
        ),
    }
    for gen in sorted(set(generations)):
        gen = owner_segment(gen)
        roots[f"generation:{gen}"] = (
            custom_root,
            user_scoped_dir(custom_root, owner.tenant_id, owner.user_id)
            / "factor_generations"
            / gen,
        )
    items: dict[str, Any] = {}
    total = 0
    for label, (root, target) in roots.items():
        path = _safe_owner_path(root, target)
        inventory = _inventory(path)
        items[label] = inventory
        total += int(inventory["bytes"])
    return {"items": items, "total_bytes": total}


def _remove_scoped_storage(
    owner: OwnerScope, task_id: str, expected: dict[str, Any]
) -> int:
    from backend.services.engine.alpha_agent.launcher import get_launcher
    from backend.shared.feature_source import quantcustom_root

    task_id = owner_segment(task_id)
    log_root = Path(get_launcher()._log_dir)
    custom_root = quantcustom_root() / "users"
    roots: dict[str, tuple[Path, Path]] = {
        "task_workspace": (
            log_root,
            user_scoped_dir(log_root, owner.tenant_id, owner.user_id)
            / task_id,
        ),
    }
    for label in expected["items"]:
        if label.startswith("generation:"):
            gen = owner_segment(label.split(":", 1)[1])
            roots[label] = (
                custom_root,
                user_scoped_dir(custom_root, owner.tenant_id, owner.user_id)
                / "factor_generations"
                / gen,
            )
    removed = 0
    for label, (root, target) in roots.items():
        path = _safe_owner_path(root, target)
        if path is None:
            continue
        current = _inventory(path)
        wanted = expected["items"].get(label) or {
            "exists": False,
            "bytes": 0,
            "entries": 0,
        }
        if current != wanted:
            raise OwnerPurgeConflict("产物文件在预览后发生变化，请重新预览")
        if path.exists():
            shutil.rmtree(path)
            removed += int(current["bytes"])
    return removed


async def _cleanup_scoped_quota(
    owner: OwnerScope, generations: list[str]
) -> dict[str, int]:
    if not generations:
        return {"objects": 0, "bytes": 0}
    conditions = " OR ".join(
        f"strpos(resource_key, :gen_{index}) > 0"
        for index in range(len(generations))
    )
    gen_params = {f"gen_{index}": gen for index, gen in enumerate(generations)}
    async with get_session() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT count(*)::bigint AS objects, "
                        "coalesce(sum(size_bytes),0)::bigint AS amount "
                        "FROM storage_quota_objects WHERE tenant_id=:tenant_id "
                        f"AND user_id=:user_id AND ({conditions})"
                    ),
                    {**owner.sql_params(), **gen_params},
                )
            )
            .mappings()
            .one()
        )
        amount = int(row["amount"])
        objects = int(row["objects"])
        if amount or objects:
            await session.execute(
                text(
                    "DELETE FROM storage_quota_objects WHERE tenant_id=:tenant_id "
                    f"AND user_id=:user_id AND ({conditions})"
                ),
                {**owner.sql_params(), **gen_params},
            )
        if amount:
            await session.execute(
                text(
                    "UPDATE storage_quota_accounts SET "
                    "used_bytes=GREATEST(0, used_bytes-:amount), updated_at=now() "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id"
                ),
                {**owner.sql_params(), "amount": amount},
            )
    return {"objects": objects, "bytes": amount}


async def _delete_scoped_database(
    owner: OwnerScope,
    task_id: str,
    purge_job: str,
    preview_token: str,
    storage_bytes: int,
) -> dict[str, int]:
    task_id = owner_segment(task_id)
    async with get_session() as session:
        await session.execute(text("SET LOCAL statement_timeout='300s'"))
        await session.execute(text("SET LOCAL lock_timeout='20s'"))
        await session.execute(
            text(
                "SELECT pool FROM resource_lease_pools "
                "WHERE pool = ANY(CAST(:pools AS TEXT[])) FOR UPDATE"
            ),
            {"pools": list(_LEASE_POOLS)},
        )
        factors, building, published, job_ids = await _scoped_identities(
            session, owner, task_id
        )
        if published:
            raise OwnerPurgeConflict("存在已发布的训练数据版本，不能单任务删除")
        counts, digests, _identities = await _scoped_snapshot_in_session(
            session, owner, task_id, purge_job
        )
        if _scoped_fingerprint(owner, task_id, counts, digests, storage_bytes) != preview_token:
            raise OwnerPurgeConflict("数据在预览后发生变化，请重新预览")
        params = _scoped_params(owner, task_id, factors, building, job_ids, purge_job)
        await session.execute(
            text(
                "UPDATE alpha_mining_tasks SET source_doc_id=NULL "
                "WHERE tenant_id=:tenant_id AND user_id=:user_id AND task_id=:task"
            ),
            params,
        )
        await session.execute(_DISABLE_TRIGGERS)
        deleted: dict[str, int] = {}

        async def wipe(label: str, statement: str) -> None:
            result = await session.execute(text(statement), params)
            deleted[label] = max(0, int(result.rowcount or 0))

        await wipe(
            "mining_materialization_outbox",
            "DELETE FROM alpha_mining_materialization_outbox "
            "WHERE tenant_id=:tenant_id AND user_id=:user_id AND task_id=:task",
        )
        for label, table in _TASK_FACTOR_TABLES:
            await wipe(
                label,
                f"DELETE FROM {table} WHERE tenant_id=:tenant_id AND user_id=:user_id "
                "AND factor_id=ANY(CAST(:factors AS TEXT[]))",
            )
        await wipe(
            "factor_edges",
            "DELETE FROM rd_agent_factor_edges WHERE tenant_id=:tenant_id "
            "AND user_id=:user_id AND (src_factor_id=ANY(CAST(:factors AS TEXT[])) "
            "OR dst_factor_id=ANY(CAST(:factors AS TEXT[])))",
        )
        await wipe(
            "factor_generation_features",
            "DELETE FROM alpha_user_factor_generation_features "
            "WHERE tenant_id=:tenant_id AND user_id=:user_id "
            "AND (generation_id=ANY(CAST(:gens AS TEXT[])) OR source_task_id=:task)",
        )
        for label, table in _TASK_GEN_TABLES[1:]:
            await wipe(
                label,
                f"DELETE FROM {table} WHERE tenant_id=:tenant_id AND user_id=:user_id "
                "AND generation_id=ANY(CAST(:gens AS TEXT[]))",
            )
        await wipe(
            "factor_job_outbox",
            "DELETE FROM alpha_factor_job_outbox WHERE tenant_id=:tenant_id "
            "AND user_id=:user_id AND job_id=ANY(CAST(:job_ids AS TEXT[])) AND job_id <> :purge_job",
        )
        await wipe(
            "factor_job_request_keys",
            "DELETE FROM alpha_factor_job_request_keys WHERE tenant_id=:tenant_id "
            "AND user_id=:user_id AND job_id=ANY(CAST(:job_ids AS TEXT[])) AND job_id <> :purge_job",
        )
        await wipe(
            "factor_jobs",
            "DELETE FROM alpha_factor_jobs WHERE tenant_id=:tenant_id "
            "AND user_id=:user_id AND job_id=ANY(CAST(:job_ids AS TEXT[])) AND job_id <> :purge_job",
        )
        for label, table in _TASK_TABLES:
            await wipe(
                label,
                f"DELETE FROM {table} WHERE tenant_id=:tenant_id AND user_id=:user_id AND task_id=:task",
            )
        # 节点分配先于租约删除：alpha_mining_node_assignments 以
        # (pool,task_id,tenant_id,user_id) 外键引用 resource_task_leases。
        await wipe(
            "node_assignments",
            "DELETE FROM alpha_mining_node_assignments "
            "WHERE tenant_id=:tenant_id AND user_id=:user_id "
            "AND ((pool='alpha_mining' AND task_id=:task) "
            "OR (pool='alpha_factor_jobs' AND task_id=ANY(CAST(:job_ids AS TEXT[]))))",
        )
        await wipe(
            "leases",
            "DELETE FROM resource_task_leases WHERE tenant_id=:tenant_id "
            "AND user_id=:user_id AND ((pool='alpha_mining' AND task_id=:task) "
            "OR (pool='alpha_factor_jobs' AND task_id=ANY(CAST(:job_ids AS TEXT[])))) "
            "AND NOT (pool='alpha_factor_jobs' AND task_id=:purge_job)",
        )
        await session.execute(_ENABLE_TRIGGERS)
    return deleted


async def _preview_scoped(
    owner: OwnerScope, task_id: str, purge_job: str = ""
) -> dict[str, Any]:
    task_id = owner_segment(task_id)
    async with get_session() as session:
        await session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        )
        counts, digests, identities = await _scoped_snapshot_in_session(
            session, owner, task_id, purge_job
        )
    blockers = await _scoped_blockers(owner, task_id, purge_job)
    storage = await asyncio.to_thread(
        _scoped_storage_inventory, owner, task_id, identities["gens"]
    )
    token = _scoped_fingerprint(owner, task_id, counts, digests, storage["total_bytes"])
    return {
        "scope": {"task_id": task_id, "factors": identities["factors"]},
        "counts": counts,
        "blockers": blockers,
        "storage": storage,
        "total_bytes": int(storage["total_bytes"]),
        "preview_token": token,
        "generated_at": to_utc_iso(utc_now()),
    }


async def _execute_scoped_purge(
    request: dict[str, Any],
    *,
    owner: OwnerScope,
    job_id: str,
    generation: int,
    jobs,
    task_id: str,
) -> dict[str, Any]:
    task_id = owner_segment(task_id)
    expected_token = request.get("preview_token")
    if not isinstance(expected_token, str) or len(expected_token) != 64:
        raise OwnerPurgeConflict("清理预览令牌无效")
    blockers = await _scoped_blockers(owner, task_id, job_id)
    if blockers:
        raise OwnerPurgeBlocked(
            "任务仍被引用或有活动作业，请先处理后再清理："
            + "，".join(f"{key}={value}" for key, value in blockers.items())
        )
    async with get_session() as session:
        _factors, building, _published, _jobs = await _scoped_identities(
            session, owner, task_id
        )
    storage = await asyncio.to_thread(_scoped_storage_inventory, owner, task_id, building)
    async with get_session() as session:
        await session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        )
        counts, digests, _identities = await _scoped_snapshot_in_session(
            session, owner, task_id, job_id
        )
    if _scoped_fingerprint(owner, task_id, counts, digests, storage["total_bytes"]) != expected_token:
        raise OwnerPurgeConflict("数据在预览后发生变化，请重新预览")
    await _progress(
        jobs, job_id, generation, owner, 15, "validate", "已校验任务快照与活动作业"
    )
    deleted = await _delete_scoped_database(
        owner, task_id, job_id, expected_token, int(storage["total_bytes"])
    )
    await _progress(
        jobs, job_id, generation, owner, 55, "database", "任务记录与因子已删除"
    )
    removed = await asyncio.to_thread(_remove_scoped_storage, owner, task_id, storage)
    await _progress(
        jobs, job_id, generation, owner, 80, "files", "任务产物已删除"
    )
    quota = await _cleanup_scoped_quota(owner, building)
    await _progress(
        jobs, job_id, generation, owner, 90, "quota", "存储配额已释放"
    )
    await asyncio.to_thread(_reconcile_budgets)
    await _progress(
        jobs, job_id, generation, owner, 99, "reconcile", "执行租约投影已对账"
    )
    return {
        "task_id": task_id,
        "deleted": deleted,
        "storage": {
            "removed_bytes": removed,
            "items": storage["items"],
        },
        "quota": quota,
        "preview_token": expected_token,
        "completed_at": to_utc_iso(utc_now()),
    }


async def preview(
    owner: OwnerScope, *, purge_job: str = "", scope_task_id: str | None = None
) -> dict[str, Any]:
    if scope_task_id:
        return await _preview_scoped(owner, scope_task_id, purge_job)
    counts, digests = await _snapshot(owner, purge_job)
    blockers = await _active_blockers(owner, purge_job)
    storage = await asyncio.to_thread(_storage_inventory, owner)
    token = _fingerprint(owner, counts, digests, storage["total_bytes"])
    return {
        "counts": counts,
        "blockers": blockers,
        "storage": storage,
        "total_bytes": int(storage["total_bytes"]),
        "preview_token": token,
        "generated_at": to_utc_iso(utc_now()),
    }


async def _progress(
    jobs,
    job_id: str,
    generation: int,
    owner: OwnerScope,
    pct: int,
    stage: str,
    detail: str,
) -> None:
    updated = await jobs.update_progress(
        job_id,
        tenant_id=owner.tenant_id,
        user_id=owner.user_id,
        generation=generation,
        progress={
            "stage": stage,
            "progress": detail,
            "progress_pct": pct,
        },
    )
    if not updated:
        raise OwnerPurgeCancelled("清理已停止或执行代次已变化")


async def execute_owner_purge(
    request: dict[str, Any],
    *,
    owner: OwnerScope,
    job_id: str,
    generation: int,
    jobs,
) -> dict[str, Any]:
    scope_task_id = request.get("task_id")
    if isinstance(scope_task_id, str) and scope_task_id:
        return await _execute_scoped_purge(
            request,
            owner=owner,
            job_id=job_id,
            generation=generation,
            jobs=jobs,
            task_id=scope_task_id,
        )
    expected_token = request.get("preview_token")
    if not isinstance(expected_token, str) or len(expected_token) != 64:
        raise OwnerPurgeConflict("清理预览令牌无效")
    blockers = await _active_blockers(owner, job_id)
    if blockers:
        raise OwnerPurgeBlocked(
            "仍有活动任务或作业，请先停止后再清理："
            + "，".join(f"{key}={value}" for key, value in blockers.items())
        )
    storage = await asyncio.to_thread(_storage_inventory, owner)
    counts, digests = await _snapshot(owner, job_id)
    if _fingerprint(owner, counts, digests, storage["total_bytes"]) != expected_token:
        raise OwnerPurgeConflict("数据在预览后发生变化，请重新预览")
    await _progress(
        jobs, job_id, generation, owner, 15, "validate", "已校验预览快照与活动作业"
    )
    deleted = await _delete_database(
        owner, job_id, expected_token, int(storage["total_bytes"])
    )
    await _progress(
        jobs, job_id, generation, owner, 55, "database", "数据库记录已删除"
    )
    removed = await asyncio.to_thread(_remove_storage, owner, storage)
    await _progress(
        jobs, job_id, generation, owner, 80, "files", "任务与文档产物已删除"
    )
    quota = await _cleanup_quota(owner)
    await _progress(
        jobs, job_id, generation, owner, 90, "quota", "存储配额已释放"
    )
    await asyncio.to_thread(_reconcile_budgets)
    await _progress(
        jobs, job_id, generation, owner, 99, "reconcile", "执行租约投影已对账"
    )
    return {
        "deleted": deleted,
        "storage": {
            "removed_bytes": removed,
            "items": storage["items"],
        },
        "quota": quota,
        "preview_token": expected_token,
        "completed_at": to_utc_iso(utc_now()),
    }
