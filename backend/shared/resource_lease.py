"""Atomic execution leases; expired leases require terminal-state reconciliation.

The readiness epoch must be restored by a durable-state reconciler, never by an
execution request. Redis loss therefore stops admission instead of overselling.
This module deliberately does not initialize or expire pool keys.
"""

import json
from dataclasses import dataclass
from uuid import uuid4

from backend.shared.owner_key import owner_key_component


class LeaseUnavailable(RuntimeError):
    """Redis or the durable reconciliation barrier is unavailable."""


_SCRIPT = """
local epoch = redis.call('GET', KEYS[1])
if not epoch or epoch ~= ARGV[1] then return {'unavailable'} end
local stamp = redis.call('TIME')
local now = tonumber(stamp[1]) * 1000 + math.floor(tonumber(stamp[2]) / 1000)
local op, task, owner, token = ARGV[2], ARGV[3], ARGV[4], ARGV[5]
local raw = redis.call('HGET', KEYS[2], task)
local row = raw and cjson.decode(raw) or nil
if op == 'acquire' then
    if row then
        if row.owner == owner and row.token == token and row.deadline > now then
            return {'held', tostring(row.deadline)}
        end
        return {'duplicate'}
    end
    local entries = redis.call('HVALS', KEYS[2])
    local count = 0
    for _, entry in ipairs(entries) do
        local item = cjson.decode(entry)
        if item.owner == owner then count = count + 1 end
    end
    if count >= tonumber(ARGV[7]) then return {'user_full'} end
    if #entries >= tonumber(ARGV[8]) then return {'global_full'} end
    local deadline = now + tonumber(ARGV[6])
    redis.call('HSET', KEYS[2], task, cjson.encode({
        owner=owner, token=token, deadline=deadline
    }))
    return {'acquired', tostring(deadline)}
end
if not row or row.owner ~= owner or row.token ~= token then return {'lost'} end
if op == 'release' then
    redis.call('HDEL', KEYS[2], task)
    return {'released'}
end
if op == 'heartbeat' then
    if row.deadline <= now then return {'lost'} end
    row.deadline = now + tonumber(ARGV[6])
    redis.call('HSET', KEYS[2], task, cjson.encode(row))
    return {'renewed', tostring(row.deadline)}
end
return {'invalid'}
"""

_RESTORE = """
redis.call('DEL', KEYS[1])
local rows = cjson.decode(ARGV[2])
local existing = redis.call('HGETALL', KEYS[2])
for i=1,#existing,2 do
    local replacement = rows[existing[i]]
    if not replacement then return 0 end
    local old = cjson.decode(existing[i+1])
    if old.token ~= replacement.token or old.owner ~= replacement.owner then
        return 0
    end
end
redis.call('DEL', KEYS[2])
local stamp = redis.call('TIME')
local now = tonumber(stamp[1])*1000 + math.floor(tonumber(stamp[2])/1000)
for task,row in pairs(rows) do
    if not row.terminal then
        row.deadline = row.suspect and 0 or now + tonumber(ARGV[3])
        row.suspect = nil
        row.terminal = nil
        redis.call('HSET', KEYS[2], task, cjson.encode(row))
    end
end
redis.call('SET', KEYS[1], ARGV[1])
return 1
"""


@dataclass(frozen=True)
class ExecutionLease:
    task_id: str
    tenant_id: str
    user_id: str
    token: str
    epoch: str


class ResourceLeasePool:
    def __init__(
        self, client, pool: str, *, per_user: int, capacity: int, ttl_ms: int = 60000
    ):
        if min(per_user, capacity, ttl_ms) <= 0:
            raise ValueError("Lease limits and heartbeat lifetime must be positive")
        self.client = client
        prefix = f"qm:lease:{{{owner_key_component(pool)}}}"
        self.ready_key = f"{prefix}:ready"
        self.records_key = f"{prefix}:records"
        self.per_user, self.capacity, self.ttl_ms = per_user, capacity, ttl_ms

    def candidate(
        self, task_id: str, tenant_id: str, user_id: str, epoch: str
    ) -> ExecutionLease:
        if not all((task_id, tenant_id, user_id, epoch)):
            raise ValueError("Task, owner and reconciliation epoch are required")
        return ExecutionLease(task_id, tenant_id, user_id, uuid4().hex, epoch)

    def _operate(self, operation: str, lease: ExecutionLease) -> str:
        owner = json.dumps([lease.tenant_id, lease.user_id], separators=(",", ":"))
        try:
            if self.client is None:
                raise LeaseUnavailable("Redis unavailable")
            result = self.client.eval(
                _SCRIPT,
                2,
                self.ready_key,
                self.records_key,
                lease.epoch,
                operation,
                lease.task_id,
                owner,
                lease.token,
                self.ttl_ms,
                self.per_user,
                self.capacity,
            )
            status = result[0]
            if isinstance(status, bytes):
                status = status.decode("ascii")
        except LeaseUnavailable:
            raise
        except Exception as exc:
            raise LeaseUnavailable("Lease operation failed") from exc
        if status == "unavailable":
            raise LeaseUnavailable("Pool awaits durable reconciliation")
        return status

    def acquire(self, lease: ExecutionLease) -> str:
        return self._operate("acquire", lease)

    def heartbeat(self, lease: ExecutionLease) -> bool:
        return self._operate("heartbeat", lease) == "renewed"

    def release(self, lease: ExecutionLease) -> bool:
        """Call only after the executor or reconciler confirms execution ended."""
        return self._operate("release", lease) == "released"

    def restore(self, epoch: str, rows: dict) -> None:
        """Called only while holding the durable pool lock, after reconciliation.

        Unknown Redis executions close the barrier rather than discard occupancy.
        The caller must reconcile such records explicitly before retrying.
        """
        try:
            restored = self.client.eval(
                _RESTORE,
                2,
                self.ready_key,
                self.records_key,
                epoch,
                json.dumps(rows),
                self.ttl_ms,
            )
        except Exception as exc:
            raise LeaseUnavailable("Lease projection restore failed") from exc
        if restored != 1:
            raise LeaseUnavailable("Unknown execution requires reconciliation")
