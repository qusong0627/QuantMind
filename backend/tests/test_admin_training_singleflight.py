"""P0-3 测试：提交侧训练单飞锁接线（admin_training_utils 的取锁/自愈/降级路径）。

覆盖契约（docs/滚动训练与模型训练生命周期_设计方案.md §4.3）：
- 空闲 → 取锁成功（值=run_id，TTL 覆盖预算+宽限）；
- 占用 → HTTPException 409（detail 带持有者 run_id）；
- 占用且持有者陈旧（终态/行丢失）→ 自愈释放后重试一次成功；
- Redis 客户端不可用 / 取锁抛异常 → **软降级放行**（串行是内存保护，不是新单点）。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import backend.services.api.routers.admin.admin_training_utils as atu
from backend.shared.training_singleflight import LOCK_KEY, lock_ttl_seconds


class FakeRedis:
    """bytes 语义的最小 redis（与真实客户端 decode_responses=False 对齐）。"""

    def __init__(self, explode_on_set: bool = False) -> None:
        self.store: dict[str, bytes] = {}
        self.expires: dict[str, object] = {}
        self.explode_on_set = explode_on_set

    def set(self, key, value, ex=None, nx=False):
        if self.explode_on_set:
            raise RuntimeError("redis down")
        if nx and key in self.store:
            return None
        self.store[key] = value.encode() if isinstance(value, str) else value
        self.expires[key] = ex
        return True

    def get(self, key):
        return self.store.get(key)

    def eval(self, script, numkeys, key, token):
        tok = token.encode() if isinstance(token, str) else token
        if self.store.get(key) == tok:
            del self.store[key]
            return 1
        return 0


def _patch_redis(monkeypatch, redis) -> None:
    import backend.shared.redis_sentinel_client as rsc

    monkeypatch.setattr(rsc, "get_redis_sentinel_client", lambda: redis)


def _occupy(redis, run_id: str) -> None:
    redis.set(LOCK_KEY, run_id, ex=1800, nx=True)


# ── 基础路径 ────────────────────────────────────────────────────────────────


def test_free_lock_acquired_with_budget_ttl(monkeypatch):
    # Arrange
    redis = FakeRedis()
    _patch_redis(monkeypatch, redis)
    # Act
    asyncio.run(atu._acquire_training_singleflight("train_A", {}))
    # Assert：值=run_id；TTL = 默认预算 120min×60 + 600（与 lock_ttl_seconds 同源）
    assert redis.get(LOCK_KEY) == b"train_A"
    assert redis.expires[LOCK_KEY] == lock_ttl_seconds(None)


def test_max_time_minutes_drives_ttl(monkeypatch):
    redis = FakeRedis()
    _patch_redis(monkeypatch, redis)
    asyncio.run(
        atu._acquire_training_singleflight("train_A", {"max_time_minutes": 240})
    )
    assert redis.expires[LOCK_KEY] == lock_ttl_seconds(240)


# ── 占用 → 409 ──────────────────────────────────────────────────────────────


def test_busy_with_live_holder_raises_409(monkeypatch):
    redis = FakeRedis()
    _patch_redis(monkeypatch, redis)
    _occupy(redis, "train_other")

    async def _alive(_run_id: str) -> bool:
        return False  # 持有者活跃（非陈旧）

    monkeypatch.setattr(atu, "_holder_job_is_stale", _alive)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(atu._acquire_training_singleflight("train_A", {}))
    assert ei.value.status_code == 409
    assert "train_other" in ei.value.detail
    # 未抢占：持有者原样
    assert redis.get(LOCK_KEY) == b"train_other"


# ── 自愈：陈旧持有者 ────────────────────────────────────────────────────────


def test_stale_holder_self_healed_and_retried(monkeypatch):
    redis = FakeRedis()
    _patch_redis(monkeypatch, redis)
    _occupy(redis, "train_dead")

    async def _stale(_run_id: str) -> bool:
        return True  # 持有者已终态/行丢失

    monkeypatch.setattr(atu, "_holder_job_is_stale", _stale)
    asyncio.run(atu._acquire_training_singleflight("train_A", {}))
    assert redis.get(LOCK_KEY) == b"train_A", "陈旧持有者应被自愈释放并由本次提交接管"


def test_self_heal_retry_can_still_lose_race(monkeypatch):
    """自愈释放后重试若仍被抢占（另一进程插队）→ 409，不是无限重试。"""
    redis = FakeRedis()
    _patch_redis(monkeypatch, redis)
    _occupy(redis, "train_dead")

    async def _stale(_run_id: str) -> bool:
        return True

    monkeypatch.setattr(atu, "_holder_job_is_stale", _stale)

    # CAS 释放（eval）完成的一瞬间，模拟他方立即插队占锁 → 我方重试失败
    real_eval = redis.eval

    def racing_eval(script, numkeys, key, token):
        result = real_eval(script, numkeys, key, token)
        redis.store[key] = b"train_inserted"
        return result

    monkeypatch.setattr(redis, "eval", racing_eval)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(atu._acquire_training_singleflight("train_A", {}))
    assert ei.value.status_code == 409
    assert "train_inserted" in ei.value.detail


# ── 软降级（fail-open）──────────────────────────────────────────────────────


def test_redis_client_unavailable_fails_open(monkeypatch):
    import backend.shared.redis_sentinel_client as rsc

    def _boom():
        raise RuntimeError("no redis")

    monkeypatch.setattr(rsc, "get_redis_sentinel_client", _boom)
    # 不抛：软降级放行（告警日志代替硬失败）
    asyncio.run(atu._acquire_training_singleflight("train_A", {}))


def test_acquire_exception_fails_open(monkeypatch):
    redis = FakeRedis(explode_on_set=True)
    _patch_redis(monkeypatch, redis)
    asyncio.run(atu._acquire_training_singleflight("train_A", {}))


# ── 释放 ────────────────────────────────────────────────────────────────────


def test_release_only_clears_own_lock(monkeypatch):
    redis = FakeRedis()
    _patch_redis(monkeypatch, redis)
    _occupy(redis, "train_other")
    atu._release_training_singleflight("train_A")  # 非属主：不生效
    assert redis.get(LOCK_KEY) == b"train_other"
    atu._release_training_singleflight("train_other")  # 属主：释放
    assert redis.get(LOCK_KEY) is None


# ── 持有者陈旧判定（真实实现，非 mock）──────────────────────────────────────


class _FakeJobSession:
    def __init__(self, record):
        self._record = record

    async def get(self, model, pk):
        return self._record

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_holder_session(monkeypatch, record=None, explode=False):
    def _get_session(read_only=False):
        if explode:
            raise RuntimeError("db down")
        return _FakeJobSession(record)

    monkeypatch.setattr(atu, "get_session", _get_session)


def test_holder_stale_on_terminal_or_missing_row(monkeypatch):
    import types

    _patch_holder_session(monkeypatch, types.SimpleNamespace(status="completed"))
    assert asyncio.run(atu._holder_job_is_stale("train_x")) is True
    _patch_holder_session(monkeypatch, types.SimpleNamespace(status="failed"))
    assert asyncio.run(atu._holder_job_is_stale("train_x")) is True
    _patch_holder_session(monkeypatch, None)  # 行丢失（上次进程崩在插入前）
    assert asyncio.run(atu._holder_job_is_stale("train_x")) is True


def test_holder_active_or_query_error_keeps_lock(monkeypatch):
    import types

    for status in ("pending", "provisioning", "running", "waiting_callback"):
        _patch_holder_session(monkeypatch, types.SimpleNamespace(status=status))
        assert asyncio.run(atu._holder_job_is_stale("train_x")) is False, status
    # 查询异常 → 保守按「占用」处理（宁可 409，不可双跑）
    _patch_holder_session(monkeypatch, explode=True)
    assert asyncio.run(atu._holder_job_is_stale("train_x")) is False


# ── HIGH-3：SET 失败但无持有者 = 基础设施故障，软降级而非 409 ────────────────


class DenySetRedis(FakeRedis):
    """SET NX 静默失败（返回 None，客户端吞异常语义）但存量可读。"""

    def __init__(self) -> None:
        super().__init__()
        self.deny_nx_calls = 0

    def set(self, key, value, ex=None, nx=False):
        if nx:
            self.deny_nx_calls += 1
            return None
        return super().set(key, value, ex=ex, nx=nx)


class FlakySetRedis(FakeRedis):
    """第一次 SET NX 失败（锁刚过期的竞态），第二次成功。"""

    def __init__(self) -> None:
        super().__init__()
        self.set_calls = 0

    def set(self, key, value, ex=None, nx=False):
        if nx:
            self.set_calls += 1
            if self.set_calls == 1:
                return None
        return super().set(key, value, ex=ex, nx=nx)


def test_set_denied_without_holder_fails_open_after_retry(monkeypatch):
    """SET 失败 + 读不到持有者 = Redis 写路径异常：重试一次仍如此 → 放行（不 409）。

    回归背景（复审 HIGH-3）：客户端 ``set`` 吞 socket 级异常返回 False，
    此时并无任何持有者；旧实现直接 409「训练进行中」，把 Redis 故障误报成
    占用，把所有提交入口锁死。
    """
    redis = DenySetRedis()
    _patch_redis(monkeypatch, redis)
    asyncio.run(atu._acquire_training_singleflight("train_A", {}))  # 不抛 = 放行
    assert redis.deny_nx_calls == 2  # 恰好多试一次


def test_transient_set_denial_recovers_on_retry(monkeypatch):
    """锁刚过期竞态：第一次 SET 失败读无持有者，重试即成功接管（不误判故障）。"""
    redis = FlakySetRedis()
    _patch_redis(monkeypatch, redis)
    asyncio.run(atu._acquire_training_singleflight("train_A", {}))
    assert redis.set_calls == 2
    assert redis.get(LOCK_KEY) == b"train_A"


def test_self_heal_then_redis_write_lost_fails_open(monkeypatch):
    """陈旧持有者自愈释放后重试仍写不进且无持有者 → 软降级放行（不 409）。"""
    redis = DenySetRedis()
    redis.store[LOCK_KEY] = b"train_dead"  # 直接落库（set 被禁，绕开写入）

    async def _stale(_run_id: str) -> bool:
        return True

    monkeypatch.setattr(atu, "_holder_job_is_stale", _stale)
    _patch_redis(monkeypatch, redis)
    asyncio.run(atu._acquire_training_singleflight("train_A", {}))  # 不抛 = 放行
    # 自愈释放确已发生（CAS 删除陈旧锁），但重取两次都写不进 → 无锁放行
    assert redis.get(LOCK_KEY) is None
    assert redis.deny_nx_calls == 2


# ── HIGH-2：提交顺序「先落 holder 行并提交，再取锁」─────────────────────────


class _RecorderSession:
    """记录 add/commit 顺序的提交侧 session（submit 只经手建行 + commit）。"""

    def __init__(self, events):
        self._events = events

    def add(self, rec):
        self._events.append(("add", rec.id))

    async def commit(self):
        self._events.append(("commit",))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeLDO:
    @staticmethod
    def _filter_features_by_parquet(run_id, features):
        return [], []


def _wire_submit(monkeypatch, events, run_id, acquire_impl):
    import backend.shared.training_singleflight as sf

    async def _fake_resolve(payload, market):
        return dict(payload), []

    monkeypatch.setattr(atu, "get_session", lambda: _RecorderSession(events))
    monkeypatch.setattr(atu, "_resolve_quantdb_factor_payload", _fake_resolve)
    monkeypatch.setattr(atu, "_normalize_payload", lambda p, f: dict(p))
    monkeypatch.setattr(atu, "_new_training_run_id", lambda: run_id)
    monkeypatch.setattr(atu, "_acquire_training_singleflight", acquire_impl)
    monkeypatch.setattr(atu, "_training_log_stream", _FakeLogStream2())
    monkeypatch.setattr(atu, "get_orchestrator", lambda node_id=None: object())
    monkeypatch.setattr(atu, "REGISTRY", _registry(events))
    monkeypatch.setattr(atu, "attach_singleflight_release", lambda t, r: None)
    monkeypatch.setattr(atu, "LocalDockerOrchestrator", _FakeLDO)
    monkeypatch.setattr(
        sf, "start_renewal", lambda rid, ttl: events.append(("renew", rid))
    )
    return sf


class _FakeLogStream2:
    def append_log(self, **kw):
        pass


def _registry(events):
    import types

    def _register(coro, run_id=None):
        coro.close()
        events.append(("register", run_id))
        return types.SimpleNamespace()

    return types.SimpleNamespace(register=_register)


def test_submit_commits_holder_row_before_acquiring_lock(monkeypatch):
    """先落行并 commit，再取锁：并发方读不到未提交行时不得误判「行丢失」自愈抢占。"""
    events = []

    async def _acquire(run_id, payload):
        events.append(("acquire", run_id))

    _wire_submit(monkeypatch, events, "train_order", _acquire)
    out = asyncio.run(
        atu.submit_training_job({}, None, {"tenant_id": "t", "user_id": "u"})
    )
    assert out["runId"] == "train_order"
    order = [e[0] for e in events]
    assert order.index("commit") < order.index("acquire"), events
    assert order.index("acquire") < order.index("renew")  # 取锁成功才启续租
    assert order.index("renew") < order.index("register")


def test_submit_409_recycles_unlaunched_row_and_skips_renewal(monkeypatch):
    """取锁被拒 409：刚落的 pending 行必须回收（否则积压幽灵任务）。"""
    events = []
    deleted = []

    async def _deny(run_id, payload):
        events.append(("acquire", run_id))
        raise HTTPException(status_code=409, detail="训练进行中")

    async def _fake_delete(run_id):
        deleted.append(run_id)

    _wire_submit(monkeypatch, events, "train_denied", _deny)
    monkeypatch.setattr(atu, "_delete_unlaunched_job", _fake_delete)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            atu.submit_training_job({}, None, {"tenant_id": "t", "user_id": "u"})
        )
    assert ei.value.status_code == 409
    assert deleted == ["train_denied"]
    assert [e[0] for e in events] == ["add", "commit", "acquire"]  # 未续租、未注册


# ── HIGH-1 次生面：取消无受管 task → 停容器兜底；迟到回调不翻转终态 ─────────


class _ScalarResult:
    def __init__(self, record):
        self._record = record

    def scalar_one_or_none(self):
        return self._record


class _TxSession:
    def __init__(self, record):
        self._record = record
        self.commits = 0

    async def execute(self, stmt):
        return _ScalarResult(self._record)

    async def commit(self):
        self.commits += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _CancelStream:
    def __init__(self):
        self.marked = []
        self.logged = []
        self.updated = []

    def mark_cancel_requested(self, run_id):
        self.marked.append(run_id)

    def append_log(self, **kw):
        self.logged.append(kw)

    def update_state(self, **kw):
        self.updated.append(kw)


def _wire_cancel(monkeypatch, record, task_cancelled):
    import types

    fallback = []
    stream = _CancelStream()
    monkeypatch.setattr(atu, "get_session", lambda: _TxSession(record))
    monkeypatch.setattr(atu, "_training_log_stream", stream)
    monkeypatch.setattr(
        atu,
        "REGISTRY",
        types.SimpleNamespace(cancel=lambda run_id: task_cancelled),
    )

    async def _fb(run_id, tenant_id, user_id):
        fallback.append((run_id, tenant_id, user_id))

    monkeypatch.setattr(atu, "_stop_run_container_best_effort", _fb)
    return stream, fallback


def test_cancel_without_managed_task_triggers_container_stop_fallback(monkeypatch):
    import types

    record = types.SimpleNamespace(
        status="running", logs="", progress=5, tenant_id="t", user_id="u"
    )
    stream, fallback = _wire_cancel(monkeypatch, record, task_cancelled=False)
    out = asyncio.run(
        atu.cancel_training_run("train_c1", {"tenant_id": "t", "user_id": "u"})
    )
    assert out == {"runId": "train_c1", "status": "cancelled", "cancelled": True}
    assert record.status == "cancelled"
    assert stream.marked == ["train_c1"]
    # 无轮询循环可读标记 → 兜底停容器必须被调用
    assert fallback == [("train_c1", "t", "u")]


def test_cancel_with_managed_task_skips_container_stop_fallback(monkeypatch):
    import types

    record = types.SimpleNamespace(
        status="running", logs="", progress=5, tenant_id="t", user_id="u"
    )
    stream, fallback = _wire_cancel(monkeypatch, record, task_cancelled=True)
    asyncio.run(atu.cancel_training_run("train_c2", {"tenant_id": "t", "user_id": "u"}))
    # 有受管 task：轮询循环会读标记自停，不需要兜底
    assert fallback == []


def test_late_callback_on_cancelled_row_is_ignored(monkeypatch):
    """取消后迟到的完成回调：不注册模型、不翻转 cancelled 终态。"""
    import types

    record = types.SimpleNamespace(
        status="cancelled", logs="旧日志\n", tenant_id="t", user_id="u", progress=50
    )
    session = _TxSession(record)
    stream = _CancelStream()
    monkeypatch.setattr(atu, "get_session", lambda: session)
    monkeypatch.setattr(atu, "_training_log_stream", stream)
    monkeypatch.setattr(atu, "_verify_internal_call_secret", lambda s: None)

    out = asyncio.run(
        atu.complete_training_run("train_late", {"status": "completed"}, "x")
    )
    assert out == {
        "ok": True,
        "runId": "train_late",
        "status": "cancelled",
        "ignored": True,
    }
    assert record.status == "cancelled"  # 未被翻回 completed
    assert "忽略迟到的完成回调" in record.logs
    assert session.commits == 1
    assert stream.updated[0]["status"] == "cancelled"
    assert stream.updated[0]["progress"] == 100
