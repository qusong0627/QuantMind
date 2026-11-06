"""PostgreSQL is authoritative for queued and executing task ownership.

Every admission transition locks the pool row. Redis is a projection, and its
loss cannot turn a durable running or suspect task into an available slot.
The session factory is injected to avoid loading platform configuration in tests.
"""

import json
from uuid import uuid4

from sqlalchemy import text

from backend.shared.process_witness import process_has_stopped

from backend.shared.resource_lease import ExecutionLease, LeaseUnavailable


class DurableLeaseStore:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def provision_pool(self, pool, *, legacy_drained=False):
        """Maintenance-only bootstrap after verified legacy executor/queue drain.

        Execution requests must never call this method or infer drain from TTL.
        Existing epochs are preserved so repeated bootstrap cannot fence running
        tasks out of their durable records.
        """
        if not legacy_drained:
            raise LeaseUnavailable("Legacy execution drain has not been verified")
        with self.session_factory() as session, session.begin():
            session.execute(
                text(
                    "INSERT INTO resource_lease_pools(pool,epoch,ready) VALUES (:pool,:epoch,false) "
                    "ON CONFLICT (pool) DO NOTHING"
                ),
                {"pool": pool, "epoch": uuid4().hex},
            )

    @staticmethod
    def _lock(session, pool):
        row = (
            session.execute(
                text(
                    "SELECT epoch, ready FROM resource_lease_pools "
                    "WHERE pool=:pool FOR UPDATE"
                ),
                {"pool": pool},
            )
            .mappings()
            .first()
        )
        if not row or not row["ready"]:
            raise LeaseUnavailable("Pool awaits durable reconciliation")
        return row["epoch"]

    def enqueue(self, pool, task_id, tenant_id, user_id, *, user_budget, total_budget):
        if (
            not all((pool, task_id, tenant_id, user_id))
            or min(user_budget, total_budget) <= 0
        ):
            raise ValueError("Identity and positive queue budgets are required")
        params = {"pool": pool, "task": task_id, "tenant": tenant_id, "user": user_id}
        with self.session_factory() as session, session.begin():
            self._lock(session, pool)
            existing = (
                session.execute(
                    text(
                        "SELECT tenant_id, user_id FROM resource_task_leases "
                        "WHERE pool=:pool AND task_id=:task"
                    ),
                    params,
                )
                .mappings()
                .first()
            )
            if existing:
                return (
                    "duplicate"
                    if (existing["tenant_id"], existing["user_id"])
                    == (tenant_id, user_id)
                    else "foreign"
                )
            counts = (
                session.execute(
                    text(
                        "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE "
                        "tenant_id=:tenant AND user_id=:user) AS owned "
                        "FROM resource_task_leases WHERE pool=:pool AND state='queued'"
                    ),
                    params,
                )
                .mappings()
                .one()
            )
            if counts["owned"] >= user_budget:
                return "user_full"
            if counts["total"] >= total_budget:
                return "global_full"
            session.execute(
                text(
                    "INSERT INTO resource_task_leases "
                    "(pool,task_id,tenant_id,user_id,state) "
                    "VALUES (:pool,:task,:tenant,:user,'queued')"
                ),
                params,
            )
            return "queued"

    def enqueue_idempotent(
        self,
        pool,
        task_id,
        tenant_id,
        user_id,
        *,
        idempotency_key,
        fingerprint,
        user_budget,
        total_budget,
    ):
        """Atomically bind an owner-scoped request key to one durable task."""
        if (
            not all((pool, task_id, tenant_id, user_id, idempotency_key, fingerprint))
            or min(user_budget, total_budget) <= 0
        ):
            raise ValueError(
                "Identity, key, fingerprint and positive budgets are required"
            )
        params = {
            "pool": pool,
            "task": task_id,
            "tenant": tenant_id,
            "user": user_id,
            "key": idempotency_key,
        }
        metadata = json.dumps(
            {"idempotency_key": idempotency_key, "fingerprint": fingerprint}
        )
        with self.session_factory() as session, session.begin():
            self._lock(session, pool)
            existing = (
                session.execute(
                    text(
                        "SELECT task_id,executor->>'fingerprint' AS fingerprint "
                        "FROM resource_task_leases WHERE pool=:pool AND tenant_id=:tenant "
                        "AND user_id=:user AND executor->>'idempotency_key'=:key "
                        "ORDER BY created_at LIMIT 1"
                    ),
                    params,
                )
                .mappings()
                .first()
            )
            if existing:
                return (
                    "existing"
                    if existing["fingerprint"] == fingerprint
                    else "conflict",
                    existing["task_id"],
                )
            counts = (
                session.execute(
                    text(
                        "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE "
                        "tenant_id=:tenant AND user_id=:user) AS owned "
                        "FROM resource_task_leases WHERE pool=:pool AND state='queued'"
                    ),
                    params,
                )
                .mappings()
                .one()
            )
            if counts["owned"] >= user_budget:
                return "user_full", task_id
            if counts["total"] >= total_budget:
                return "global_full", task_id
            session.execute(
                text(
                    "INSERT INTO resource_task_leases "
                    "(pool,task_id,tenant_id,user_id,state,executor) "
                    "VALUES (:pool,:task,:tenant,:user,'queued',CAST(:metadata AS jsonb))"
                ),
                {**params, "metadata": metadata},
            )
            return "queued", task_id

    def enqueue_transactional(
        self,
        pool,
        task_id,
        tenant_id,
        user_id,
        *,
        user_budget,
        total_budget,
        prepare,
        persist,
    ):
        """Admit a queue lease and its business row in one locked transaction.

        ``prepare(session)`` may return an existing/conflict outcome before queue
        limits are charged. ``persist(session)`` writes the business row after
        the durable lease insert; any exception rolls both writes back.
        """
        if (
            not all((pool, task_id, tenant_id, user_id))
            or min(user_budget, total_budget) <= 0
            or not callable(prepare)
            or not callable(persist)
        ):
            raise ValueError(
                "Identity, budgets and transactional callbacks are required"
            )
        params = {
            "pool": pool,
            "task": task_id,
            "tenant": tenant_id,
            "user": user_id,
        }
        with self.session_factory() as session, session.begin():
            self._lock(session, pool)
            prepared = prepare(session)
            if prepared is not None:
                return prepared
            counts = (
                session.execute(
                    text(
                        "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE "
                        "tenant_id=:tenant AND user_id=:user) AS owned "
                        "FROM resource_task_leases WHERE pool=:pool AND state='queued'"
                    ),
                    params,
                )
                .mappings()
                .one()
            )
            if counts["owned"] >= user_budget:
                return "user_full", task_id
            if counts["total"] >= total_budget:
                return "global_full", task_id
            session.execute(
                text(
                    "INSERT INTO resource_task_leases "
                    "(pool,task_id,tenant_id,user_id,state) "
                    "VALUES (:pool,:task,:tenant,:user,'queued')"
                ),
                params,
            )
            persist(session)
            return "queued", task_id

    def reserve(
        self,
        pool,
        task_id,
        tenant_id,
        user_id,
        *,
        per_user,
        capacity,
        executor=None,
        fair=False,
    ):
        if min(per_user, capacity) <= 0:
            raise ValueError("Positive execution limits are required")
        params = {"pool": pool, "task": task_id, "tenant": tenant_id, "user": user_id}
        with self.session_factory() as session, session.begin():
            epoch = self._lock(session, pool)
            row = (
                session.execute(
                    text(
                        "SELECT state FROM resource_task_leases WHERE pool=:pool "
                        "AND task_id=:task AND tenant_id=:tenant AND user_id=:user"
                    ),
                    params,
                )
                .mappings()
                .first()
            )
            if not row or row["state"] != "queued":
                return None
            if fair:
                # Only the oldest queued task per currently idle owner competes
                # for the next slot. A user's later tasks cannot sit ahead of
                # other owners, and a user already at its active limit does not
                # block everyone else.
                waiting_owner = session.execute(
                    text(
                        "SELECT 1 FROM resource_task_leases AS candidate "
                        "WHERE candidate.pool=:pool AND candidate.state='queued' "
                        "AND (candidate.created_at,candidate.task_id) < ("
                        "SELECT current_task.created_at,current_task.task_id FROM resource_task_leases AS current_task "
                        "WHERE current_task.pool=:pool AND current_task.task_id=:task) "
                        "AND NOT EXISTS (SELECT 1 FROM resource_task_leases AS earlier "
                        "WHERE earlier.pool=:pool AND earlier.state='queued' "
                        "AND earlier.tenant_id=candidate.tenant_id AND earlier.user_id=candidate.user_id "
                        "AND (earlier.created_at,earlier.task_id) < (candidate.created_at,candidate.task_id)) "
                        "AND NOT EXISTS (SELECT 1 FROM resource_task_leases AS active "
                        "WHERE active.pool=:pool AND active.tenant_id=candidate.tenant_id "
                        "AND active.user_id=candidate.user_id "
                        "AND active.state IN ('reserved','running','suspect')) LIMIT 1"
                    ),
                    {**params},
                ).first()
                if waiting_owner:
                    return None
            counts = (
                session.execute(
                    text(
                        "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE "
                        "tenant_id=:tenant AND user_id=:user) AS owned FROM resource_task_leases "
                        "WHERE pool=:pool AND state IN ('reserved','running','suspect')"
                    ),
                    params,
                )
                .mappings()
                .one()
            )
            if counts["owned"] >= per_user or counts["total"] >= capacity:
                return None
            token = uuid4().hex
            session.execute(
                text(
                    "UPDATE resource_task_leases SET state='reserved', token=:token, "
                    "epoch=:epoch, executor=CAST(:executor AS jsonb), updated_at=NOW(), heartbeat_at=NOW() "
                    "WHERE pool=:pool AND task_id=:task"
                ),
                {
                    **params,
                    "token": token,
                    "epoch": epoch,
                    "executor": json.dumps(executor or {}),
                },
            )
            return ExecutionLease(task_id, tenant_id, user_id, token, epoch)

    def reserve_transactional(
        self,
        pool,
        task_id,
        tenant_id,
        user_id,
        *,
        per_user,
        capacity,
        persist,
        executor=None,
    ):
        """Reserve a queued lease and update its business row in one transaction."""
        if min(per_user, capacity) <= 0 or not callable(persist):
            raise ValueError("Positive execution limits and callback are required")
        params = {"pool": pool, "task": task_id, "tenant": tenant_id, "user": user_id}
        with self.session_factory() as session, session.begin():
            epoch = self._lock(session, pool)
            row = (
                session.execute(
                    text(
                        "SELECT state FROM resource_task_leases WHERE pool=:pool "
                        "AND task_id=:task AND tenant_id=:tenant AND user_id=:user FOR UPDATE"
                    ),
                    params,
                )
                .mappings()
                .first()
            )
            if not row or row["state"] != "queued":
                return None
            counts = (
                session.execute(
                    text(
                        "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE "
                        "tenant_id=:tenant AND user_id=:user) AS owned "
                        "FROM resource_task_leases WHERE pool=:pool "
                        "AND state IN ('reserved','running','suspect')"
                    ),
                    params,
                )
                .mappings()
                .one()
            )
            if counts["owned"] >= per_user or counts["total"] >= capacity:
                return None
            token = uuid4().hex
            updated = session.execute(
                text(
                    "UPDATE resource_task_leases SET state='reserved',token=:token,"
                    "epoch=:epoch,executor=CAST(:executor AS jsonb),updated_at=NOW(),"
                    "heartbeat_at=NOW() WHERE pool=:pool AND task_id=:task "
                    "AND tenant_id=:tenant AND user_id=:user AND state='queued'"
                ),
                {
                    **params,
                    "token": token,
                    "epoch": epoch,
                    "executor": json.dumps(executor or {}),
                },
            )
            if updated.rowcount != 1:
                return None
            persist(session)
            return ExecutionLease(task_id, tenant_id, user_id, token, epoch)

    def cancel_queued(self, pool, task_id, tenant_id, user_id):
        """Cancel before execution; running work needs executor termination proof."""
        with self.session_factory() as session, session.begin():
            session.execute(
                text(
                    "SELECT pool FROM resource_lease_pools WHERE pool=:pool FOR UPDATE"
                ),
                {"pool": pool},
            ).first()
            result = session.execute(
                text(
                    "UPDATE resource_task_leases SET state='cancelled', updated_at=NOW() "
                    "WHERE pool=:pool AND task_id=:task AND tenant_id=:tenant AND user_id=:user "
                    "AND state='queued'"
                ),
                {"pool": pool, "task": task_id, "tenant": tenant_id, "user": user_id},
            )
            return result.rowcount == 1

    def task_state(
        self, pool, task_id, tenant_id, user_id, *, expected_dispatch_token=None
    ):
        with self.session_factory() as session:
            return session.execute(
                text(
                    "SELECT state FROM resource_task_leases WHERE pool=:pool "
                    "AND task_id=:task AND tenant_id=:tenant AND user_id=:user "
                    "AND (:dispatch IS NULL OR executor->>'dispatch_token'=:dispatch)"
                ),
                {
                    "pool": pool,
                    "task": task_id,
                    "tenant": tenant_id,
                    "user": user_id,
                    "dispatch": expected_dispatch_token,
                },
            ).scalar_one_or_none()

    def get_lease(
        self, pool, task_id, tenant_id, user_id, *, expected_dispatch_token=None
    ):
        """Return an active owner's durable lease for a trusted lifecycle observer."""
        with self.session_factory() as session:
            row = (
                session.execute(
                    text(
                        "SELECT token,epoch,state FROM resource_task_leases "
                        "WHERE pool=:pool AND task_id=:task AND tenant_id=:tenant "
                        "AND user_id=:user AND state IN ('reserved','running','suspect')"
                        " AND (:dispatch IS NULL OR executor->>'dispatch_token'=:dispatch)"
                    ),
                    {
                        "pool": pool,
                        "task": task_id,
                        "tenant": tenant_id,
                        "user": user_id,
                        "dispatch": expected_dispatch_token,
                    },
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            return ExecutionLease(
                task_id, tenant_id, user_id, row["token"], row["epoch"]
            )

    def task_snapshot(self, pool, task_id, tenant_id, user_id):
        """Trusted lifecycle evidence; callers expose only public state fields."""
        with self.session_factory() as session:
            row = (
                session.execute(
                    text(
                        "SELECT state,executor FROM resource_task_leases WHERE pool=:pool "
                        "AND task_id=:task AND tenant_id=:tenant AND user_id=:user"
                    ),
                    {
                        "pool": pool,
                        "task": task_id,
                        "tenant": tenant_id,
                        "user": user_id,
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            return {
                "state": row["state"],
                "stop_requested": bool(row["executor"].get("stop_requested")),
                "executor": dict(row["executor"]),
            }

    def claim_engine_execution(
        self,
        pool,
        lease,
        *,
        dispatch_token,
        request_id=None,
        engine_process,
        execution_generation,
        allow_reclaim_stopped=False,
    ):
        """Give one engine request exclusive authority to start a durable sandbox."""
        request_id = request_id or dispatch_token
        if (
            not dispatch_token
            or len(dispatch_token) > 128
            or not request_id
            or len(request_id) > 128
            or not engine_process
            or type(execution_generation) is not int
            or execution_generation <= 0
        ):
            raise ValueError("Engine execution claim requires a dispatch witness")
        params = {
            "pool": pool,
            "task": lease.task_id,
            "tenant": lease.tenant_id,
            "user": lease.user_id,
            "token": lease.token,
            "epoch": lease.epoch,
            "dispatch": dispatch_token,
            "request_id": request_id,
            "generation": execution_generation,
            "engine_process": json.dumps(engine_process),
        }
        with self.session_factory() as session, session.begin():
            if not session.execute(
                text(
                    "SELECT pool FROM resource_lease_pools WHERE pool=:pool FOR UPDATE"
                ),
                params,
            ).first():
                raise LeaseUnavailable("Factor job pool missing")
            row = (
                session.execute(
                    text(
                        "SELECT state,executor FROM resource_task_leases WHERE pool=:pool "
                        "AND task_id=:task AND tenant_id=:tenant AND user_id=:user "
                        "AND token=:token AND epoch=:epoch FOR UPDATE"
                    ),
                    params,
                )
                .mappings()
                .first()
            )
            if not row or row["state"] != "running":
                return "inactive"
            executor = row["executor"] or {}
            existing_token = executor.get("engine_claim_token")
            if existing_token:
                if existing_token != dispatch_token:
                    return "existing_other"
                old_process = executor.get("engine_process")
                if (
                    allow_reclaim_stopped
                    and isinstance(old_process, dict)
                    and process_has_stopped(old_process)
                ):
                    result = session.execute(
                        text(
                            "UPDATE resource_task_leases SET executor=jsonb_set(executor,"
                            "'{engine_process}',CAST(:engine_process AS jsonb),true) || "
                            "jsonb_build_object('engine_request_id',:request_id,"
                            "'engine_request_state','active'),"
                            "updated_at=now() WHERE pool=:pool AND task_id=:task "
                            "AND tenant_id=:tenant AND user_id=:user AND token=:token "
                            "AND epoch=:epoch AND state='running' "
                            "AND executor->>'engine_claim_token'=:dispatch "
                            "AND executor->>'execution_generation'=:generation_text "
                            "AND executor->'engine_process'=CAST(:old_engine_process AS jsonb)"
                        ),
                        {
                            **params,
                            "generation_text": str(execution_generation),
                            "old_engine_process": json.dumps(old_process),
                        },
                    )
                    if result.rowcount == 1:
                        return "reclaimed"
                    return "inactive"
                if executor.get("stop_requested"):
                    return "stop_requested"
                if (
                    allow_reclaim_stopped
                    and executor.get("engine_request_state") == "finished"
                ):
                    result = session.execute(
                        text(
                            "UPDATE resource_task_leases SET executor=jsonb_set(executor,"
                            "'{engine_process}',CAST(:engine_process AS jsonb),true) || "
                            "jsonb_build_object('engine_request_id',:request_id,"
                            "'engine_request_state','active'),updated_at=now() "
                            "WHERE pool=:pool AND task_id=:task AND tenant_id=:tenant "
                            "AND user_id=:user AND token=:token AND epoch=:epoch "
                            "AND state='running' AND executor->>'engine_claim_token'=:dispatch "
                            "AND executor->>'execution_generation'=:generation_text "
                            "AND executor->>'engine_request_state'='finished' "
                            "AND executor->>'stop_requested' IS DISTINCT FROM 'true'"
                        ),
                        {**params, "generation_text": str(execution_generation)},
                    )
                    return "reclaimed" if result.rowcount == 1 else "inactive"
                return "existing_same"
            if (executor or {}).get("stop_requested"):
                return "stop_requested"
            result = session.execute(
                text(
                    "UPDATE resource_task_leases SET executor=executor || "
                    "jsonb_build_object('engine_claim_token',:dispatch,'engine_process',"
                    "CAST(:engine_process AS jsonb),'execution_generation',:generation,"
                    "'engine_request_id',:request_id,'engine_request_state','active'),"
                    "updated_at=now() "
                    "WHERE pool=:pool AND task_id=:task AND tenant_id=:tenant "
                    "AND user_id=:user AND token=:token AND epoch=:epoch AND state='running' "
                    "AND executor->>'engine_claim_token' IS NULL "
                    "AND executor->>'stop_requested' IS DISTINCT FROM 'true'"
                ),
                params,
            )
            return "claimed" if result.rowcount == 1 else "inactive"

    def mark_engine_request_finished(
        self, pool, lease, *, dispatch_token, request_id, execution_generation
    ):
        """Record that one trusted engine HTTP execution has fully unwound."""
        if (
            not dispatch_token
            or not request_id
            or type(execution_generation) is not int
            or execution_generation <= 0
        ):
            raise ValueError("Engine request completion requires its execution claim")
        with self.session_factory() as session, session.begin():
            result = session.execute(
                text(
                    "UPDATE resource_task_leases SET executor=jsonb_set(executor,"
                    "'{engine_request_state}','\"finished\"'::jsonb,true),"
                    "updated_at=now() WHERE pool=:pool AND task_id=:task "
                    "AND tenant_id=:tenant AND user_id=:user AND token=:token "
                    "AND epoch=:epoch AND state='running' "
                    "AND executor->>'engine_claim_token'=:dispatch "
                    "AND executor->>'engine_request_id'=:request_id "
                    "AND executor->>'execution_generation'=:generation_text "
                    "AND executor->>'engine_request_state'='active'"
                ),
                {
                    "pool": pool,
                    "task": lease.task_id,
                    "tenant": lease.tenant_id,
                    "user": lease.user_id,
                    "token": lease.token,
                    "epoch": lease.epoch,
                    "dispatch": dispatch_token,
                    "request_id": request_id,
                    "generation_text": str(execution_generation),
                },
            )
            return result.rowcount == 1

    def engine_execution_candidates(self, pool):
        """List active engine-owned leases for exact, fail-closed crash recovery."""
        with self.session_factory() as session:
            rows = (
                session.execute(
                    text(
                        "SELECT task_id,tenant_id,user_id,token,epoch,state,executor "
                        "FROM resource_task_leases WHERE pool=:pool "
                        "AND state IN ('reserved','running','suspect') "
                        "AND executor ? 'engine_process'"
                    ),
                    {"pool": pool},
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]

    def finalize_orphaned_engine_execution(
        self, pool, candidate, *, terminal_state, on_transition
    ):
        """Atomically reconcile an engine lease after exact Docker stop proof."""
        if terminal_state not in {"failed", "cancelled"} or not callable(on_transition):
            raise ValueError("Invalid orphaned execution reconciliation")
        params = {
            "pool": pool,
            "task": candidate.get("task_id"),
            "tenant": candidate.get("tenant_id"),
            "user": candidate.get("user_id"),
            "token": candidate.get("token"),
            "epoch": candidate.get("epoch"),
        }
        with self.session_factory() as session, session.begin():
            session.execute(
                text(
                    "SELECT pool FROM resource_lease_pools WHERE pool=:pool FOR UPDATE"
                ),
                params,
            ).first()
            row = (
                session.execute(
                    text(
                        "SELECT state,executor FROM resource_task_leases WHERE pool=:pool "
                        "AND task_id=:task AND tenant_id=:tenant AND user_id=:user "
                        "AND token=:token AND epoch=:epoch FOR UPDATE"
                    ),
                    params,
                )
                .mappings()
                .first()
            )
            if (
                row is None
                or row["state"] not in {"reserved", "running", "suspect"}
                or row["executor"] != candidate.get("executor")
            ):
                return None
            executor = row["executor"] or {}
            if executor.get("rdagent_child") is not None:
                return None
            actual_state = (
                "cancelled" if executor.get("stop_requested") else terminal_state
            )
            on_transition(session, actual_state, executor)
            updated = session.execute(
                text(
                    "UPDATE resource_task_leases SET state=:state,updated_at=now() "
                    "WHERE pool=:pool AND task_id=:task AND tenant_id=:tenant "
                    "AND user_id=:user AND token=:token AND epoch=:epoch "
                    "AND state IN ('reserved','running','suspect')"
                ),
                {**params, "state": actual_state},
            )
            if updated.rowcount != 1:
                raise LeaseUnavailable("Orphaned engine lease changed during recovery")
            return ExecutionLease(
                params["task"],
                params["tenant"],
                params["user"],
                params["token"],
                params["epoch"],
            )

    def request_stop(self, pool, task_id, tenant_id, user_id):
        """Record intent; only unstarted tasks become terminal immediately."""
        params = {"pool": pool, "task": task_id, "tenant": tenant_id, "user": user_id}
        with self.session_factory() as session, session.begin():
            session.execute(
                text(
                    "SELECT pool FROM resource_lease_pools WHERE pool=:pool FOR UPDATE"
                ),
                params,
            )
            row = (
                session.execute(
                    text(
                        "SELECT state FROM resource_task_leases WHERE pool=:pool AND task_id=:task "
                        "AND tenant_id=:tenant AND user_id=:user"
                    ),
                    params,
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            state = row["state"]
            if state == "queued":
                session.execute(
                    text(
                        "UPDATE resource_task_leases SET state='cancelled', updated_at=NOW() "
                        "WHERE pool=:pool AND task_id=:task"
                    ),
                    params,
                )
                return "cancelled"
            if state in {"reserved", "running", "suspect"}:
                session.execute(
                    text(
                        "UPDATE resource_task_leases SET executor=executor || jsonb_build_object('stop_requested',TRUE), "
                        "updated_at=NOW() WHERE pool=:pool AND task_id=:task"
                    ),
                    params,
                )
                return "stop_requested"
            return state

    def request_stop_transactional(
        self, pool, task_id, tenant_id, user_id, *, on_transition
    ):
        """Fence a business cancellation with its durable queue lease update."""
        if not all((pool, task_id, tenant_id, user_id)) or not callable(on_transition):
            raise ValueError("Owner identity and cancellation callback are required")
        params = {"pool": pool, "task": task_id, "tenant": tenant_id, "user": user_id}
        with self.session_factory() as session, session.begin():
            pool_row = session.execute(
                text(
                    "SELECT pool FROM resource_lease_pools WHERE pool=:pool FOR UPDATE"
                ),
                params,
            ).first()
            if not pool_row:
                return None
            row = (
                session.execute(
                    text(
                        "SELECT state,executor FROM resource_task_leases WHERE pool=:pool "
                        "AND task_id=:task AND tenant_id=:tenant AND user_id=:user FOR UPDATE"
                    ),
                    params,
                )
                .mappings()
                .first()
            )
            if not row:
                return None
            state = row["state"]
            if state == "queued":
                updated = session.execute(
                    text(
                        "UPDATE resource_task_leases SET state='cancelled',updated_at=NOW() "
                        "WHERE pool=:pool AND task_id=:task AND tenant_id=:tenant "
                        "AND user_id=:user AND state='queued'"
                    ),
                    params,
                )
                if updated.rowcount != 1:
                    raise LeaseUnavailable(
                        "Queued factor job lease changed during cancel"
                    )
                on_transition(session, "cancelled")
                return "cancelled"
            if state in {"reserved", "running", "suspect"}:
                updated = session.execute(
                    text(
                        "UPDATE resource_task_leases SET executor=executor || "
                        "jsonb_build_object('stop_requested',TRUE),updated_at=NOW() "
                        "WHERE pool=:pool AND task_id=:task AND tenant_id=:tenant "
                        "AND user_id=:user AND state=:state"
                    ),
                    {**params, "state": state},
                )
                if updated.rowcount != 1:
                    raise LeaseUnavailable(
                        "Active factor job lease changed during cancel"
                    )
                on_transition(session, "stopping")
                return "stop_requested"
            return state

    def confirm_process_stopped(self, pool, task_id, witness):
        from backend.shared.process_witness import process_has_stopped

        if not process_has_stopped(witness):
            return None
        return self._confirm_stopped(pool, task_id, witness)

    def confirm_container_stopped(self, pool, task_id, witness, client):
        from backend.shared.container_witness import container_namespace_has_stopped

        if not container_namespace_has_stopped(client, witness):
            return None
        return self._confirm_stopped(pool, task_id, witness)

    def _confirm_stopped(self, pool, task_id, witness):
        with self.session_factory() as session, session.begin():
            params = {"pool": pool, "task": task_id}
            session.execute(
                text(
                    "SELECT pool FROM resource_lease_pools WHERE pool=:pool FOR UPDATE"
                ),
                params,
            )
            row = (
                session.execute(
                    text(
                        "SELECT tenant_id,user_id,token,epoch,state,executor FROM resource_task_leases "
                        "WHERE pool=:pool AND task_id=:task"
                    ),
                    params,
                )
                .mappings()
                .first()
            )
            if not row or row["state"] not in {"reserved", "running", "suspect"}:
                return None
            if row["executor"].get("process") != witness:
                return None
            if row["executor"].get("rdagent_child") is not None:
                return None
            state = "cancelled" if row["executor"].get("stop_requested") else "failed"
            session.execute(
                text(
                    "UPDATE resource_task_leases SET state=:state, updated_at=NOW() "
                    "WHERE pool=:pool AND task_id=:task"
                ),
                {**params, "state": state},
            )
            return ExecutionLease(
                task_id, row["tenant_id"], row["user_id"], row["token"], row["epoch"]
            )

    def expire_queued(self, pool, *, max_age_seconds):
        if max_age_seconds <= 0:
            raise ValueError("Queue maximum age must be positive")
        with self.session_factory() as session, session.begin():
            params = {"pool": pool, "age": max_age_seconds}
            session.execute(
                text(
                    "SELECT pool FROM resource_lease_pools WHERE pool=:pool FOR UPDATE"
                ),
                params,
            )
            rows = (
                session.execute(
                    text(
                        "UPDATE resource_task_leases SET state='failed', updated_at=NOW() "
                        "WHERE pool=:pool AND state='queued' "
                        "AND created_at < NOW() - make_interval(secs => :age) "
                        "RETURNING task_id,tenant_id,user_id"
                    ),
                    params,
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]

    def queued_tasks(self, pool, *, min_age_seconds=30, limit=64):
        """List stable queue entries for owner-side recovery after process restart."""
        if min_age_seconds < 0 or limit <= 0:
            raise ValueError("Invalid queued task recovery bounds")
        with self.session_factory() as session:
            rows = (
                session.execute(
                    text(
                        "SELECT task_id,tenant_id,user_id FROM resource_task_leases "
                        "WHERE pool=:pool AND state='queued' "
                        "AND created_at < NOW() - make_interval(secs => :age) "
                        "ORDER BY created_at,task_id LIMIT :limit"
                    ),
                    {"pool": pool, "age": min_age_seconds, "limit": limit},
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]

    def execution_witnesses(self, pool):
        with self.session_factory() as session:
            rows = (
                session.execute(
                    text(
                        "SELECT task_id,executor FROM resource_task_leases WHERE pool=:pool "
                        "AND state IN ('reserved','running','suspect')"
                    ),
                    {"pool": pool},
                )
                .mappings()
                .all()
            )
            return [(row["task_id"], row["executor"].get("process")) for row in rows]

    def active_executions(self, pool):
        """Return owner-scoped durable execution witnesses for a trusted reconciler."""
        with self.session_factory() as session:
            rows = (
                session.execute(
                    text(
                        "SELECT task_id,tenant_id,user_id,token,epoch,state,executor,updated_at "
                        "FROM resource_task_leases WHERE pool=:pool "
                        "AND state IN ('reserved','running','suspect')"
                    ),
                    {"pool": pool},
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]

    def transition(
        self, pool, lease, state, *, executor=None, expected_dispatch_token=None
    ):
        if state not in {"running", "suspect", "completed", "failed", "cancelled"}:
            raise ValueError("Unsupported lease state")
        # Suspect executions remain counted. Terminal states are sticky.
        allowed = "('reserved','running','suspect')"
        if state == "running":
            allowed = "('reserved','running')"
        params = {
            "pool": pool,
            "task": lease.task_id,
            "tenant": lease.tenant_id,
            "user": lease.user_id,
            "token": lease.token,
            "epoch": lease.epoch,
            "state": state,
            "executor": json.dumps(executor) if executor is not None else None,
            "expected_dispatch_token": expected_dispatch_token,
        }
        with self.session_factory() as session, session.begin():
            row = session.execute(
                text(
                    "SELECT pool FROM resource_lease_pools WHERE pool=:pool FOR UPDATE"
                ),
                params,
            ).first()
            if not row:
                raise LeaseUnavailable("Pool missing")
            result = session.execute(
                text(
                    "UPDATE resource_task_leases SET state=:state, updated_at=NOW(), "
                    "heartbeat_at=CASE WHEN :state='running' THEN NOW() ELSE heartbeat_at END, "
                    "executor=executor || COALESCE(CAST(:executor AS jsonb),'{}'::jsonb) "
                    "WHERE pool=:pool AND task_id=:task AND tenant_id=:tenant AND user_id=:user "
                    "AND token=:token AND epoch=:epoch "
                    "AND (:expected_dispatch_token IS NULL OR executor->>'dispatch_token'=:expected_dispatch_token) "
                    "AND (:state <> 'running' OR executor->>'stop_requested' IS DISTINCT FROM 'true') "
                    "AND (:state IN ('running','suspect') OR executor->'rdagent_child' IS NULL) "
                    "AND state IN " + allowed
                ),
                params,
            )
            return result.rowcount == 1

    def claim_child_execution(self, pool, lease, child_id, *, operation):
        """Atomically claim the one nested executor slot under a parent lease."""
        if (
            not isinstance(child_id, str)
            or not child_id
            or not isinstance(operation, str)
        ):
            raise ValueError("A child execution id and operation are required")
        params = {
            "pool": pool,
            "task": lease.task_id,
            "tenant": lease.tenant_id,
            "user": lease.user_id,
            "token": lease.token,
            "epoch": lease.epoch,
            "child": json.dumps({"id": child_id, "operation": operation}),
        }
        with self.session_factory() as session, session.begin():
            result = session.execute(
                text(
                    "UPDATE resource_task_leases SET "
                    "executor=executor || jsonb_build_object('rdagent_child',CAST(:child AS jsonb)), "
                    "updated_at=NOW(),heartbeat_at=NOW() "
                    "WHERE pool=:pool AND task_id=:task AND tenant_id=:tenant "
                    "AND user_id=:user AND token=:token AND epoch=:epoch "
                    "AND state='running' AND executor->'rdagent_child' IS NULL "
                    "AND executor->>'stop_requested' IS DISTINCT FROM 'true'"
                ),
                params,
            )
            return result.rowcount == 1

    def release_child_execution(self, pool, lease, child_id):
        """Clear only the matching nested execution marker for its lease owner."""
        params = {
            "pool": pool,
            "task": lease.task_id,
            "tenant": lease.tenant_id,
            "user": lease.user_id,
            "token": lease.token,
            "epoch": lease.epoch,
            "child": child_id,
        }
        with self.session_factory() as session, session.begin():
            result = session.execute(
                text(
                    "UPDATE resource_task_leases SET executor=executor-'rdagent_child', "
                    "updated_at=NOW(),heartbeat_at=NOW() "
                    "WHERE pool=:pool AND task_id=:task AND tenant_id=:tenant "
                    "AND user_id=:user AND token=:token AND epoch=:epoch "
                    "AND executor->'rdagent_child'->>'id'=:child "
                    "AND state IN ('running','suspect')"
                ),
                params,
            )
            return result.rowcount == 1

    def finish_child_execution_request(self, pool, lease, child_id):
        """Mark a child handler finished so the reconciler can safely clean its resources."""
        params = {
            "pool": pool,
            "task": lease.task_id,
            "tenant": lease.tenant_id,
            "user": lease.user_id,
            "token": lease.token,
            "epoch": lease.epoch,
            "child": child_id,
        }
        with self.session_factory() as session, session.begin():
            result = session.execute(
                text(
                    "UPDATE resource_task_leases SET executor=jsonb_set(executor, "
                    "'{rdagent_child,request_finished}', 'true'::jsonb, TRUE), "
                    "updated_at=NOW(),heartbeat_at=NOW() "
                    "WHERE pool=:pool AND task_id=:task AND tenant_id=:tenant "
                    "AND user_id=:user AND token=:token AND epoch=:epoch "
                    "AND executor->'rdagent_child'->>'id'=:child "
                    "AND state IN ('running','suspect')"
                ),
                params,
            )
            return result.rowcount == 1

    def transition_transactional(
        self,
        pool,
        lease,
        state,
        *,
        expected_states,
        persist,
    ):
        """Commit a durable lease transition with its owner business update."""
        supported = {"running", "suspect", "completed", "failed", "cancelled"}
        allowed_states = set(expected_states)
        if (
            state not in supported
            or not allowed_states
            or allowed_states - {"reserved", "running", "suspect"}
            or not callable(persist)
        ):
            raise ValueError("Invalid transactional lease transition")
        params = {
            "pool": pool,
            "task": lease.task_id,
            "tenant": lease.tenant_id,
            "user": lease.user_id,
            "token": lease.token,
            "epoch": lease.epoch,
            "state": state,
            "expected": sorted(allowed_states),
        }
        with self.session_factory() as session, session.begin():
            if not session.execute(
                text(
                    "SELECT pool FROM resource_lease_pools WHERE pool=:pool FOR UPDATE"
                ),
                params,
            ).first():
                raise LeaseUnavailable("Pool missing")
            current = (
                session.execute(
                    text(
                        "SELECT state,executor FROM resource_task_leases WHERE pool=:pool "
                        "AND task_id=:task AND tenant_id=:tenant AND user_id=:user "
                        "AND token=:token AND epoch=:epoch FOR UPDATE"
                    ),
                    params,
                )
                .mappings()
                .first()
            )
            if not current or current["state"] not in allowed_states:
                return False
            if (
                state in {"completed", "failed", "cancelled"}
                and (current["executor"] or {}).get("rdagent_child") is not None
            ):
                return False
            persist(session)
            updated = session.execute(
                text(
                    "UPDATE resource_task_leases SET state=:state,updated_at=NOW(), "
                    "heartbeat_at=CASE WHEN :state='running' THEN NOW() ELSE heartbeat_at END "
                    "WHERE pool=:pool AND task_id=:task AND tenant_id=:tenant "
                    "AND user_id=:user AND token=:token AND epoch=:epoch "
                    "AND (:state <> 'running' OR executor->>'stop_requested' IS DISTINCT FROM 'true') "
                    "AND (:state IN ('running','suspect') OR executor->'rdagent_child' IS NULL) "
                    "AND state=ANY(:expected)"
                ),
                params,
            )
            if updated.rowcount != 1:
                raise LeaseUnavailable("Lease changed during transactional transition")
            return True

    def confirm_running(self, pool, lease, *, dispatch_token):
        """Reactivate a suspect training lease only after executor identity proof.

        The caller must first verify the persisted dispatch claim and observe the
        exact executor alive. The token predicate prevents an old observer from
        reviving a lease belonging to a newer launch attempt.
        """
        if not dispatch_token:
            return False
        params = {
            "pool": pool,
            "task": lease.task_id,
            "tenant": lease.tenant_id,
            "user": lease.user_id,
            "token": lease.token,
            "epoch": lease.epoch,
            "dispatch": dispatch_token,
        }
        with self.session_factory() as session, session.begin():
            session.execute(
                text(
                    "SELECT pool FROM resource_lease_pools WHERE pool=:pool FOR UPDATE"
                ),
                params,
            ).first()
            result = session.execute(
                text(
                    "UPDATE resource_task_leases SET state='running', heartbeat_at=NOW(), "
                    "updated_at=NOW() WHERE pool=:pool AND task_id=:task "
                    "AND tenant_id=:tenant AND user_id=:user AND token=:token AND epoch=:epoch "
                    "AND state IN ('reserved','running','suspect') "
                    "AND executor->>'dispatch_token'=:dispatch"
                ),
                params,
            )
            return result.rowcount == 1

    def restore_projection(self, pool, projection):
        """Serialize durable reservations and restore their Redis projection.

        Provisioning the initial pool row and draining old counter-based workers
        is a deployment prerequisite. This method never guesses legacy ownership.
        """
        failure = None
        with self.session_factory() as session, session.begin():
            pool_row = (
                session.execute(
                    text(
                        "SELECT epoch FROM resource_lease_pools WHERE pool=:pool FOR UPDATE"
                    ),
                    {"pool": pool},
                )
                .mappings()
                .first()
            )
            if not pool_row:
                raise LeaseUnavailable("Pool has not been provisioned")
            session.execute(
                text(
                    "UPDATE resource_task_leases SET state='suspect', updated_at=NOW() "
                    "WHERE pool=:pool AND state IN ('reserved','running') "
                    "AND heartbeat_at < NOW() - make_interval(secs => :seconds)"
                ),
                {"pool": pool, "seconds": projection.ttl_ms / 1000},
            )
            existing = list(projection.client.hkeys(projection.records_key))
            existing = [
                item.decode() if isinstance(item, bytes) else item for item in existing
            ]
            rows = (
                session.execute(
                    text(
                        "SELECT task_id,tenant_id,user_id,token,state FROM resource_task_leases "
                        "WHERE pool=:pool AND token IS NOT NULL AND "
                        "(state IN ('reserved','running','suspect') OR task_id=ANY(:existing))"
                    ),
                    {"pool": pool, "existing": existing},
                )
                .mappings()
                .all()
            )
            snapshot = {
                row["task_id"]: {
                    "owner": json.dumps(
                        [row["tenant_id"], row["user_id"]], separators=(",", ":")
                    ),
                    "token": row["token"],
                    "suspect": row["state"] == "suspect",
                    "terminal": row["state"] in {"completed", "failed", "cancelled"},
                }
                for row in rows
            }
            try:
                projection.restore(pool_row["epoch"], snapshot)
            except LeaseUnavailable as exc:
                failure = exc
            session.execute(
                text(
                    "UPDATE resource_lease_pools SET ready=:ready, updated_at=NOW() WHERE pool=:pool"
                ),
                {"pool": pool, "ready": failure is None},
            )
        if failure is not None:
            raise failure
