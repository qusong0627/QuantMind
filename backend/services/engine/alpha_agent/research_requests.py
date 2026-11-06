"""PG-owned short LLM requests: no heavy queue, no automatic paid replay."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid

from sqlalchemy import text

from backend.services.engine.alpha_agent.research_budget import (
    POOL,
    daily_limits,
    get_budget,
    require_ready_budget,
)
from backend.shared.owner_scope import OwnerScope
from backend.shared.process_witness import capture_process, process_has_stopped
from backend.shared.resource_lease import LeaseUnavailable
from backend.shared.secret_redaction import redact_text
from backend.shared.utc_datetime import to_utc_iso, utc_now

logger = logging.getLogger(__name__)
_controllers: set[asyncio.Task] = (
    set()
)  # strong references only; PG owns all business state
_TERMINAL = {"completed", "failed", "cancelled"}


class ResearchConflict(ValueError):
    pass


class ResearchBusy(ValueError):
    pass


class ResearchStopped(RuntimeError):
    pass


def _encoded(value):
    result = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(result.encode()) > 128 * 1024:
        raise ValueError("研究请求或结果超过 128 KiB")
    return result


def get(owner: OwnerScope, request_id: str):
    with get_budget().store.session_factory() as session:
        row = (
            session.execute(
                text(
                    "SELECT * FROM alpha_research_requests WHERE tenant_id=:tenant_id AND user_id=:user_id AND request_id=:id"
                ),
                {**owner.sql_params(), "id": request_id},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None


def state(owner: OwnerScope, request_id: str):
    with get_budget().store.session_factory() as session:
        return session.execute(
            text(
                "SELECT status FROM alpha_research_requests WHERE tenant_id=:tenant_id AND user_id=:user_id AND request_id=:id"
            ),
            {**owner.sql_params(), "id": request_id},
        ).scalar_one_or_none()


def existing(owner: OwnerScope, key: str, kind: str, submission: dict):
    with get_budget().store.session_factory() as session:
        row = (
            session.execute(
                text(
                    "SELECT * FROM alpha_research_requests WHERE tenant_id=:tenant_id AND user_id=:user_id AND request_key=:key"
                ),
                {**owner.sql_params(), "key": key},
            )
            .mappings()
            .first()
        )
        if row:
            if (
                row["kind"] != kind
                or row["request_snapshot"].get("submission") != submission
            ):
                raise ResearchConflict("幂等键已用于不同研究请求")
            return dict(row)
    return None


def by_key(owner: OwnerScope, key: str):
    with get_budget().store.session_factory() as session:
        row = (
            session.execute(
                text(
                    "SELECT * FROM alpha_research_requests WHERE tenant_id=:tenant_id AND user_id=:user_id AND request_key=:key"
                ),
                {**owner.sql_params(), "key": key},
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None


def public(row: dict, *, include_result=True):
    snapshot = row["request_snapshot"]
    result = {
        key: row[key]
        for key in (
            "request_id",
            "kind",
            "status",
            "error_code",
            "error_message",
            "call_count",
        )
    }
    result.update(
        created_at=to_utc_iso(row["created_at"]),
        completed_at=to_utc_iso(row["completed_at"]) if row["completed_at"] else None,
        label=snapshot.get("factor_name") or snapshot.get("direction", "")[:120],
        factor_id=snapshot.get("factor_id"),
        universe=snapshot.get("universe"),
        source_revision_no=snapshot.get("source", {}).get("revision_no"),
    )
    if include_result:
        result["result"] = row["result_snapshot"]
    return result


def history(owner: OwnerScope, offset: int):
    with get_budget().store.session_factory() as session:
        rows = (
            session.execute(
                text(
                    "SELECT * FROM alpha_research_requests WHERE tenant_id=:tenant_id AND user_id=:user_id "
                    "ORDER BY created_at DESC,request_id DESC LIMIT 21 OFFSET :offset"
                ),
                {**owner.sql_params(), "offset": offset},
            )
            .mappings()
            .all()
        )
        return {
            "items": [public(dict(row), include_result=False) for row in rows[:20]],
            "has_more": len(rows) > 20,
        }


def cancel(owner: OwnerScope, request_id: str):
    budget = get_budget()

    def persist(session, status):
        updated = session.execute(
            text(
                "UPDATE alpha_research_requests SET status=:status,completed_at=CASE WHEN :status='cancelled' THEN now() ELSE completed_at END "
                "WHERE tenant_id=:tenant_id AND user_id=:user_id AND request_id=:id "
                "AND status IN ('queued','running','stopping')"
            ),
            {**owner.sql_params(), "id": request_id, "status": status},
        )
        if updated.rowcount != 1:
            raise ResearchConflict("研究请求已变化")

    return budget.store.request_stop_transactional(
        POOL, request_id, owner.tenant_id, owner.user_id, on_transition=persist
    )


def _admit(owner: OwnerScope, key: str, kind: str, snapshot: dict):
    if (
        kind not in {"explain", "decompose"}
        or not key
        or len(key) > 128
        or any(ord(c) < 32 or ord(c) == 127 for c in key)
    ):
        raise ValueError("无效的研究请求或 Idempotency-Key")
    encoded = _encoded({"kind": kind, **snapshot})
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    request_id = uuid.uuid4().hex
    budget = require_ready_budget()

    def prepare(session):
        existing = (
            session.execute(
                text(
                    "SELECT request_id,kind,request_snapshot FROM alpha_research_requests WHERE tenant_id=:tenant_id AND user_id=:user_id AND request_key=:key"
                ),
                {**owner.sql_params(), "key": key},
            )
            .mappings()
            .first()
        )
        if existing:
            if (
                existing["kind"] != kind
                or existing["request_snapshot"].get("submission")
                != snapshot["submission"]
            ):
                raise ResearchConflict(
                    "幂等键已用于不同的研究输入或版本；请查看原请求记录"
                )
            return "existing", existing["request_id"]

    def persist(session):
        session.execute(
            text(
                "INSERT INTO alpha_research_requests (tenant_id,user_id,request_id,request_key,fingerprint,kind,status,request_snapshot,controller_json) "
                "VALUES (:tenant_id,:user_id,:id,:key,:fingerprint,:kind,'queued',CAST(:snapshot AS jsonb),CAST(:controller AS jsonb))"
            ),
            {
                **owner.sql_params(),
                "id": request_id,
                "key": key,
                "fingerprint": fingerprint,
                "kind": kind,
                "snapshot": _encoded(snapshot),
                "controller": _encoded(capture_process()),
            },
        )

    outcome, admitted = budget.store.enqueue_transactional(
        POOL,
        request_id,
        owner.tenant_id,
        owner.user_id,
        user_budget=1,
        total_budget=4,
        prepare=prepare,
        persist=persist,
    )
    if outcome in {"user_full", "global_full"}:
        raise ResearchBusy("研究请求繁忙，请稍后重试")
    return admitted, outcome == "queued"


def _reserve(owner, request_id):
    budget = require_ready_budget()
    witness = capture_process()

    def persist(session):
        updated = session.execute(
            text(
                "UPDATE alpha_research_requests SET status='running' WHERE tenant_id=:tenant_id AND user_id=:user_id AND request_id=:id AND status='queued'"
            ),
            {**owner.sql_params(), "id": request_id},
        )
        if updated.rowcount != 1:
            raise ResearchStopped("研究请求已取消")

    lease = budget.store.reserve_transactional(
        POOL,
        request_id,
        owner.tenant_id,
        owner.user_id,
        per_user=budget.projection.per_user,
        capacity=budget.projection.capacity,
        persist=persist,
        executor={
            "kind": "research_llm",
            "engine_process": witness,
            "dispatch_token": request_id,
            "engine_claim_token": request_id,
            "engine_request_id": request_id,
            "engine_request_state": "active",
            "execution_generation": 1,
        },
    )
    if lease is None:
        if state(owner, request_id) == "cancelled":
            raise ResearchStopped("研究请求已在开始前取消")
        cancel(owner, request_id)
        raise ResearchBusy("研究模型调用名额已满；本次未调用模型")
    return lease


def _meter(owner, request_id, lease, messages):
    budget = require_ready_budget()
    if not budget.projection.heartbeat(lease):
        raise LeaseUnavailable("研究请求租约已失效")
    user_limit, global_limit = daily_limits()
    day = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    prompt_hash = hashlib.sha256(_encoded(messages).encode()).hexdigest()

    def persist(session):
        row = (
            session.execute(
                text(
                    "SELECT status,request_snapshot FROM alpha_research_requests WHERE tenant_id=:tenant_id AND user_id=:user_id AND request_id=:id FOR UPDATE"
                ),
                {**owner.sql_params(), "id": request_id},
            )
            .mappings()
            .one()
        )
        if row["status"] != "running":
            raise ResearchStopped("研究请求已停止")
        # Global pool lock is held by transition_transactional; counts and charge serialize.
        counts = (
            session.execute(
                text(
                    "SELECT count(*) AS total,count(*) FILTER (WHERE tenant_id=:tenant_id AND user_id=:user_id) AS owned "
                    "FROM alpha_research_llm_calls WHERE started_at>=:day"
                ),
                {**owner.sql_params(), "day": day},
            )
            .mappings()
            .one()
        )
        ordinal = session.execute(
            text(
                "SELECT count(*)+1 FROM alpha_research_llm_calls WHERE tenant_id=:tenant_id AND user_id=:user_id AND request_id=:id"
            ),
            {**owner.sql_params(), "id": request_id},
        ).scalar_one()
        if (
            counts["owned"] >= user_limit
            or counts["total"] >= global_limit
            or ordinal > 2
        ):
            raise ResearchBusy("研究模型调用预算已用尽；未派发本次调用")
        session.execute(
            text(
                "INSERT INTO alpha_research_llm_calls (tenant_id,user_id,request_id,ordinal,model,prompt_sha256) VALUES (:tenant_id,:user_id,:id,:ordinal,:model,:prompt)"
            ),
            {
                **owner.sql_params(),
                "id": request_id,
                "ordinal": ordinal,
                "model": row["request_snapshot"]["model"],
                "prompt": prompt_hash,
            },
        )
        session.execute(
            text(
                "UPDATE alpha_research_requests SET call_count=:ordinal WHERE tenant_id=:tenant_id AND user_id=:user_id AND request_id=:id AND status='running'"
            ),
            {**owner.sql_params(), "id": request_id, "ordinal": ordinal},
        )

    if not budget.store.transition_transactional(
        POOL, lease, "running", expected_states=("reserved", "running"), persist=persist
    ):
        raise ResearchStopped("研究执行租约已变化")


def _finish(
    owner, request_id, lease, *, result=None, error=None, stopped=False, on_finish=None
):
    budget = get_budget()
    status = "cancelled" if stopped else "failed" if error else "completed"
    encoded = _encoded(result) if result is not None else None

    def persist(session):
        row = (
            session.execute(
                text(
                    "SELECT status FROM alpha_research_requests WHERE tenant_id=:tenant_id AND user_id=:user_id AND request_id=:id FOR UPDATE"
                ),
                {**owner.sql_params(), "id": request_id},
            )
            .mappings()
            .one()
        )
        if row["status"] == "stopping" and status != "cancelled":
            raise ResearchStopped("研究请求已停止，迟到结果不发布")
        if row["status"] not in {"running", "stopping"}:
            raise ResearchConflict("研究请求已终结")
        if status == "completed" and on_finish is not None:
            on_finish(session, result)
        session.execute(
            text(
                "UPDATE alpha_research_requests SET status=:status,result_snapshot=CAST(:result AS jsonb),error_code=:code,error_message=:error,completed_at=now() "
                "WHERE tenant_id=:tenant_id AND user_id=:user_id AND request_id=:id AND status IN ('running','stopping')"
            ),
            {
                **owner.sql_params(),
                "id": request_id,
                "status": status,
                "result": encoded,
                "code": type(error).__name__ if error else None,
                "error": redact_text(str(error))[:1000] if error else None,
            },
        )

    changed = budget.store.transition_transactional(
        POOL,
        lease,
        status,
        expected_states=("reserved", "running", "suspect"),
        persist=persist,
    )
    if changed:
        try:
            budget.projection.release(lease)
        except Exception:
            pass  # terminal PG row is authoritative; projection repair is maintenance work


async def _drive(owner, key, kind, snapshot, action, on_finish):
    request_id, fresh = await asyncio.to_thread(_admit, owner, key, kind, snapshot)
    if not fresh:
        return await asyncio.to_thread(get, owner, request_id)
    lease = None
    operation = None
    watcher = None
    budget = get_budget()
    try:
        lease = await asyncio.to_thread(_reserve, owner, request_id)
        if await asyncio.to_thread(budget.projection.acquire, lease) not in {
            "acquired",
            "held",
        }:
            raise LeaseUnavailable("研究请求投影暂不可用")

        async def meter(messages):
            await asyncio.to_thread(_meter, owner, request_id, lease, messages)

        async def watch():
            ticks = 0
            while True:
                await asyncio.sleep(1)
                status = await asyncio.to_thread(state, owner, request_id)
                if status != "running":
                    raise ResearchStopped("研究请求已停止")
                ticks += 1
                if ticks % 10 == 0:
                    if not await asyncio.to_thread(budget.projection.heartbeat, lease):
                        raise LeaseUnavailable("研究请求租约不可用")
                    if not await asyncio.to_thread(
                        budget.store.transition, POOL, lease, "running"
                    ):
                        raise ResearchStopped("研究请求执行围栏已变化")

        operation = asyncio.create_task(asyncio.wait_for(action(meter), timeout=250))
        watcher = asyncio.create_task(watch())
        finished, _ = await asyncio.wait(
            (operation, watcher), return_when=asyncio.FIRST_COMPLETED
        )
        if watcher in finished:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            watcher.result()
        result = operation.result()
        await asyncio.to_thread(
            _finish, owner, request_id, lease, result=result, on_finish=on_finish
        )
    except ResearchStopped as exc:
        if lease:
            await asyncio.to_thread(cancel, owner, request_id)
            await asyncio.to_thread(
                _finish, owner, request_id, lease, error=exc, stopped=True
            )
    except Exception as exc:
        if lease:
            try:
                await asyncio.to_thread(_finish, owner, request_id, lease, error=exc)
            except ResearchStopped:
                await asyncio.to_thread(
                    _finish, owner, request_id, lease, error=exc, stopped=True
                )
        else:
            raise
    finally:
        # Never publish a stop proof while a local HTTP/model coroutine is live.
        for task in (operation, watcher):
            if task is not None and not task.done():
                task.cancel()
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except BaseException:
                        if task.done():
                            break
        if lease is None:
            # Admission may succeed before Redis/PG reservation becomes unavailable.
            # No model coroutine was started: cancel the owned queue entry rather
            # than leaving it behind until this engine process exits. If an
            # ambiguous reservation committed, request_stop retains its occupancy
            # until recovery has exact execution-stop evidence.
            try:
                await asyncio.to_thread(cancel, owner, request_id)
            except Exception:
                logger.warning(
                    "Unstarted research request cancellation deferred; occupancy retained"
                )
        if lease and (operation is None or operation.done()):
            await asyncio.to_thread(
                budget.store.mark_engine_request_finished,
                POOL,
                lease,
                dispatch_token=request_id,
                request_id=request_id,
                execution_generation=1,
            )
    return await asyncio.to_thread(get, owner, request_id)


async def run(
    owner: OwnerScope, *, key: str, kind: str, snapshot: dict, action, on_finish=None
):
    task = asyncio.create_task(_drive(owner, key, kind, snapshot, action, on_finish))
    _controllers.add(task)

    def done(finished):
        _controllers.discard(finished)
        if not finished.cancelled():
            finished.exception()  # disconnected callers still have a durable outcome

    task.add_done_callback(done)
    return await asyncio.shield(task)


def reconcile():
    """No timeout/TTL proof and no paid replay: only explicit finish or process exit."""
    budget = get_budget()
    for candidate in budget.store.engine_execution_candidates(POOL):
        executor = candidate.get("executor") or {}
        if executor.get("kind") != "research_llm":
            continue
        if executor.get(
            "engine_request_state"
        ) != "finished" and not process_has_stopped(
            executor.get("engine_process") or {}
        ):
            continue
        owner = OwnerScope(candidate["tenant_id"], candidate["user_id"])

        def persist(session, status, witness):
            updated = session.execute(
                text(
                    "UPDATE alpha_research_requests SET status=:status,error_code='ResearchInterrupted',"
                    "error_message='原研究请求已结束或进程退出；不会自动重试模型调用',completed_at=now() "
                    "WHERE tenant_id=:tenant_id AND user_id=:user_id AND request_id=:id AND status IN ('running','stopping')"
                ),
                {**owner.sql_params(), "id": candidate["task_id"], "status": status},
            )
            if updated.rowcount != 1:
                raise ResearchConflict("研究状态已变化")

        lease = budget.store.finalize_orphaned_engine_execution(
            POOL, candidate, terminal_state="failed", on_transition=persist
        )
        if lease:
            try:
                budget.projection.release(lease)
            except Exception:
                pass
    for pending in budget.store.queued_tasks(POOL, min_age_seconds=30):
        owner = OwnerScope(pending["tenant_id"], pending["user_id"])
        row = get(owner, pending["task_id"])
        if row and process_has_stopped(row["controller_json"]):
            cancel(owner, row["request_id"])
    budget.store.restore_projection(POOL, budget.projection)
