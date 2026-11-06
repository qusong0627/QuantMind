"""Shared lifecycle for durable admission, heartbeat and confirmed termination."""

import asyncio
import threading
from contextlib import contextmanager

from backend.shared.resource_lease import LeaseUnavailable


class ExecutionCapacityFull(RuntimeError):
    """The queued task could not acquire its execution budget."""


class ResourceExecution:
    def __init__(
        self, store, projection, pool, *, heartbeat_seconds=15, fair_reservation=False
    ):
        if heartbeat_seconds <= 0 or heartbeat_seconds * 1000 >= projection.ttl_ms:
            raise ValueError("Heartbeat interval must be shorter than lease lifetime")
        self.store, self.projection, self.pool = store, projection, pool
        self.heartbeat_seconds = heartbeat_seconds
        self.fair_reservation = fair_reservation

    @contextmanager
    def running(
        self,
        task_id,
        tenant_id,
        user_id,
        *,
        executor=None,
        preserve_on_stop=False,
        terminal_resolver=None,
    ):
        lease = self.store.reserve(
            self.pool,
            task_id,
            tenant_id,
            user_id,
            per_user=self.projection.per_user,
            capacity=self.projection.capacity,
            executor=executor,
            fair=self.fair_reservation,
        )
        if lease is None:
            raise ExecutionCapacityFull(
                "Execution budget unavailable or task not queued"
            )
        terminal = "failed"
        stopped = threading.Event()
        thread = None

        def heartbeat():
            while not stopped.wait(self.heartbeat_seconds):
                try:
                    if not self.projection.heartbeat(lease):
                        raise LeaseUnavailable("Execution lease lost")
                    if not self.store.transition(self.pool, lease, "running"):
                        raise LeaseUnavailable("Durable execution lease lost")
                except Exception:
                    # Keep occupancy while computation finishes. Never recover a
                    # suspect execution to running merely because Redis returns.
                    try:
                        self.store.transition(self.pool, lease, "suspect")
                    except Exception:
                        pass  # Existing durable reservation remains occupied.
                    return

        try:
            if self.projection.acquire(lease) not in {"acquired", "held"}:
                raise LeaseUnavailable("Lease projection disagrees with reservation")
            if not self.store.transition(
                self.pool, lease, "running", executor=executor
            ):
                raise LeaseUnavailable("Durable reservation no longer owned")
            new_thread = threading.Thread(
                target=heartbeat, daemon=True, name=f"lease-heartbeat-{task_id}"
            )
            new_thread.start()
            thread = new_thread
            yield lease
            terminal = "completed"
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # An interrupted await can leave its thread/container still running.
            # Keep the reservation until an executor completion is observed.
            terminal = None
            raise
        finally:
            stopped.set()
            if thread is not None:
                # Do not release while a heartbeat can still change durable state.
                thread.join(timeout=max(1.0, self.heartbeat_seconds))
            if thread is not None and thread.is_alive():
                # Retain occupancy for terminal-state reconciliation. Avoid
                # masking the computation's original exception or result.
                return_cleanup = False
            else:
                return_cleanup = True
            if return_cleanup:
                try:
                    snapshot = self.store.task_snapshot(
                        self.pool, task_id, tenant_id, user_id
                    )
                    if snapshot and snapshot["stop_requested"]:
                        if terminal_resolver is not None:
                            terminal = terminal_resolver(lease, snapshot)
                        else:
                            terminal = None if preserve_on_stop else "cancelled"
                    elif terminal_resolver is not None:
                        terminal = terminal_resolver(lease, snapshot)
                    confirmed = (
                        self.store.transition(self.pool, lease, terminal or "suspect")
                        and terminal is not None
                    )
                except Exception:
                    confirmed = False
                if confirmed:
                    try:
                        self.projection.release(lease)
                    except LeaseUnavailable:
                        pass  # Durable terminal state permits later reconciliation.
            # Cancellation/worker death that cannot reach here retains its slot.
            # A caller must confirm the executor ended before terminal recovery.
