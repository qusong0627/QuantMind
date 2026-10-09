"""P0-3 测试：训练单飞锁（宿主内存 ~44G 真上限 → 训练必须串行的代码化）。

背景（docs/滚动训练与模型生命周期_设计方案.md §4.3）：
多入口（UI / 外部门面 / 批跑器 / 重训调度）可并发提交训练，44G 级内存
进程互杀（全局 OOM 挑受害者不分青红皂白）。单飞锁把「串行」从纪律变成代码：
- 键 `qm:training:singleflight`，SET NX EX，值 = run_id（属主）；
- TTL = max_time_minutes×60 + 600（回调等待宽限），下限 1800；
- 释放走 Lua CAS（属主校验，防过期后误删他人锁）；
- 占用中再提交 → 409；持有者已终态/行丢失 → 自愈释放重试。
"""

from __future__ import annotations

import asyncio

import backend.shared.training_singleflight as ts
from backend.shared.training_singleflight import (
    ACTIVE_STATUSES,
    LOCK_KEY,
    get_holder,
    holder_is_stale,
    lock_ttl_seconds,
    release,
    renew,
    try_acquire,
)


class FakeRedis:
    """SET NX EX + CAS 释放（eval 语义与 redis_lock.RELEASE_LUA 一致）。

    忠实模拟真实客户端 ``decode_responses=False``：SET 后落库是 **bytes**，
    GET 返回 bytes，CAS 在「存量 bytes」与「调用方 str」之间按 redis 编码
    语义归一——``holder()`` 曾在这里踩雷（``str(b"x") == "b'x'"``）。
    """

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.expires: dict[str, object] = {}

    @staticmethod
    def _enc(value):
        return value.encode() if isinstance(value, str) else value

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = self._enc(value)
        self.expires[key] = ex
        return True

    def get(self, key):
        return self.store.get(key)

    def eval(self, script, numkeys, key, token):
        if self.store.get(key) == self._enc(token):
            del self.store[key]
            return 1
        return 0


# ── TTL ─────────────────────────────────────────────────────────────────────


def test_ttl_covers_budget_plus_callback_grace():
    assert lock_ttl_seconds(120) == 120 * 60 + 600
    assert lock_ttl_seconds(240) == 240 * 60 + 600


def test_ttl_default_and_floor():
    # 缺省预算 120 分钟（与编排器默认一致）
    assert lock_ttl_seconds(None) == 120 * 60 + 600
    # 0 与缺省同义（编排器 `or 120` 同口径），不是「零预算」
    assert lock_ttl_seconds(0) == 120 * 60 + 600
    # 小预算不产生过短 TTL（下限 1800s）
    assert lock_ttl_seconds(5) == 1800


# ── 获取 / 释放 ─────────────────────────────────────────────────────────────


def test_acquire_and_release_cycle():
    redis = FakeRedis()
    assert try_acquire(redis, "train_A", 1800) is True
    assert get_holder(redis) == "train_A"
    assert redis.expires[LOCK_KEY] == 1800
    # 占用中再取 → 失败（值不动）
    assert try_acquire(redis, "train_B", 1800) is False
    assert get_holder(redis) == "train_A"
    # 错误属主释放 → 不生效（CAS）
    assert release(redis, "train_B") is False
    assert get_holder(redis) == "train_A"
    # 正确属主释放 → 成功
    assert release(redis, "train_A") is True
    assert get_holder(redis) is None
    # 释放后可再次获取
    assert try_acquire(redis, "train_C", 1800) is True


def test_get_holder_empty():
    assert get_holder(FakeRedis()) is None


def test_holder_bytes_decoded_and_reusable_as_release_token():
    """bytes 客户端下 holder 必须解码成明文 token，且该值可直接用于 CAS 释放。

    回归背景：``str(b"train_A")`` = ``"b'train_A'"``，把这种值当 token 回传时
    CAS 永远不等 → 提交侧「陈旧持有者自愈释放」静默失效（锁卡到 TTL）。
    """
    redis = FakeRedis()
    assert try_acquire(redis, "train_A", 1800) is True
    holder = get_holder(redis)
    assert holder == "train_A"  # 不是 "b'train_A'"
    assert release(redis, holder) is True  # 自愈路径：拿到持有者值即能 CAS 释放
    assert get_holder(redis) is None


# ── 陈旧判据（自愈依据，纯函数）────────────────────────────────────────────


def test_holder_is_stale_matrix():
    for status in ACTIVE_STATUSES:
        assert holder_is_stale(status) is False, status
    for status in ("completed", "failed", "cancelled", ""):
        assert holder_is_stale(status) is True, status
    # 行丢失（DB 查不到）→ 陈旧
    assert holder_is_stale(None) is True
    assert ACTIVE_STATUSES == ("pending", "provisioning", "running", "waiting_callback")


# ── 续租心跳（复审 HIGH-4）──────────────────────────────────────────────────


class RenewFakeRedis(FakeRedis):
    """追加 RENEW_LUA 语义（GET==token → EXPIRE）；按参数个数与释放区分。"""

    def __init__(self) -> None:
        super().__init__()
        self.eval_calls: list[tuple] = []

    def eval(self, script, numkeys, *args):
        self.eval_calls.append(args)
        if len(args) == 3:  # RENEW_LUA: key, token, ttl
            key, token, ttl = args[0], args[1], int(args[2])
            if self.store.get(key) == self._enc(token):
                self.expires[key] = ttl
                return 1
            return 0
        return super().eval(script, numkeys, *args)


def _patch_renew_target(monkeypatch, redis, interval=0.01):
    import backend.shared.redis_sentinel_client as rsc

    monkeypatch.setattr(rsc, "get_redis_sentinel_client", lambda: redis)
    monkeypatch.setattr(ts, "RENEW_INTERVAL_SECONDS", interval)


def test_renew_cas_only_for_owner():
    redis = RenewFakeRedis()
    assert try_acquire(redis, "train_A", 1800) is True
    # 非属主续租 → 拒绝，TTL 不变（不能把易主后的锁续成自己的）
    assert renew(redis, "train_B", 3600) is False
    assert redis.expires[LOCK_KEY] == 1800
    # 属主续租 → 成功且 TTL 拉长
    assert renew(redis, "train_A", 3600) is True
    assert redis.expires[LOCK_KEY] == 3600
    # 键已释放 → 续租拒绝（心跳将据此自终止）
    release(redis, "train_A")
    assert renew(redis, "train_A", 3600) is False


def test_renew_loop_self_terminates_after_two_consecutive_misses(monkeypatch):
    """锁已释放/易主 → 连续 2 次续租被拒即自终止（防任务泄漏）。"""
    redis = RenewFakeRedis()  # 空库：每次续租都返回 0

    async def _run():
        _patch_renew_target(monkeypatch, redis)
        ts.start_renewal("train_miss", 1800)
        task = ts._RENEWERS["train_miss"]
        await asyncio.wait_for(task, timeout=2)
        await asyncio.sleep(0)  # done_callback 清账
        assert "train_miss" not in ts._RENEWERS

    try:
        asyncio.run(_run())
    finally:
        ts.stop_renewal("train_miss")
    assert len(redis.eval_calls) == 2  # 恰好两次失手，不多打


def test_renew_loop_resets_misses_on_success(monkeypatch):
    """失手计数按「连续」算：一次成功即清零，交替失手/成功不退出。"""
    calls = {"n": 0}

    class _Alternating:
        def eval(self, script, numkeys, *args):
            calls["n"] += 1
            return 0 if calls["n"] % 2 == 1 else 1  # 0,1,0,1,... 永无连击

    async def _run():
        _patch_renew_target(monkeypatch, _Alternating())
        ts.start_renewal("train_alt", 1800)
        task = ts._RENEWERS["train_alt"]
        try:
            await asyncio.sleep(0.1)  # ≥6 个 tick
            assert not task.done(), "交替失手不应触发自终止（miss 未按连续计）"
            assert calls["n"] >= 6
        finally:
            ts.stop_renewal("train_alt")
            await asyncio.sleep(0)  # 取消生效
            assert "train_alt" not in ts._RENEWERS

    asyncio.run(_run())


def test_renew_loop_redis_errors_do_not_count_as_misses(monkeypatch):
    """eval 异常（连接抖动）不记失手：Redis 恢复后仍应续上，循环不退出。"""

    class _Raising:
        def __init__(self):
            self.calls = 0

        def eval(self, *a):
            self.calls += 1
            raise ConnectionError("redis hiccup")

    raising = _Raising()

    async def _run():
        _patch_renew_target(monkeypatch, raising)
        ts.start_renewal("train_err", 1800)
        task = ts._RENEWERS["train_err"]
        try:
            await asyncio.sleep(0.08)
            assert not task.done()  # 异常 ≠ 失手，循环仍在
            assert raising.calls >= 4
        finally:
            ts.stop_renewal("train_err")
            await asyncio.sleep(0)

    asyncio.run(_run())


def test_start_renewal_idempotent_and_loop_cancel_swallowed(monkeypatch):
    redis = RenewFakeRedis()
    assert try_acquire(redis, "train_A", 1800) is True

    async def _run():
        _patch_renew_target(monkeypatch, redis)
        ts.start_renewal("train_A", 1800)
        t1 = ts._RENEWERS["train_A"]
        ts.start_renewal("train_A", 1800)  # 同 run 幂等：同一 task
        assert ts._RENEWERS["train_A"] is t1
        await asyncio.sleep(0.03)  # 让循环真跑起来（进入 sleep 点后再取消）
        assert not t1.done() and not t1.cancelled()
        ts.stop_renewal("train_A")
        assert "train_A" not in ts._RENEWERS
        await t1  # 循环内 except CancelledError → return；await 不抛 = 吞取消正确
        assert t1.done() and not t1.cancelled()

    try:
        asyncio.run(_run())
    finally:
        ts.stop_renewal("train_A")


def test_start_renewal_without_running_loop_is_noop():
    """同步上下文（无运行 loop）启动续租 → 告警跳过，不崩溃、不注册。"""
    ts._RENEWERS.pop("train_sync", None)
    ts.start_renewal("train_sync", 1800)
    assert "train_sync" not in ts._RENEWERS
    ts.stop_renewal("train_sync")  # 不存在也幂等


def test_retire_renewer_identity_guard_keeps_newer_task():
    """NEW-6：旧心跳的迟到 done 回调不得把字典里的**新任务**摘除。

    竞态：旧任务已 done 但回调未及运行（call_soon 排队）时 start_renewal 建了
    新任务覆盖 _RENEWERS[key]；无身份判断的旧回调会把新任务摘出字典 →
    stop_renewal 找不到、再进 start_renewal 重复建心跳。
    """
    old, new = object(), object()
    ts._RENEWERS["train_x"] = new
    try:
        ts._retire_renewer("train_x", old)  # 迟到的旧回调
        assert ts._RENEWERS.get("train_x") is new  # 新任务不动
        ts._retire_renewer("train_x", new)  # 本人回调
        assert "train_x" not in ts._RENEWERS
    finally:
        ts._RENEWERS.pop("train_x", None)


def test_start_renewal_after_old_task_done_registers_new(monkeypatch):
    """端到端竞态复刻：旧任务 done 后立刻重启续租，注册的必须是新任务。"""
    redis = RenewFakeRedis()
    assert try_acquire(redis, "train_A", 1800) is True

    async def _run():
        _patch_renew_target(monkeypatch, redis)
        ts.start_renewal("train_A", 1800)
        t1 = ts._RENEWERS["train_A"]
        await asyncio.sleep(0.03)
        ts.stop_renewal("train_A")
        await asyncio.sleep(0)  # t1 取消生效（此时 done 回调可能尚未跑）
        ts.start_renewal("train_A", 1800)  # 立刻重启
        t2 = ts._RENEWERS.get("train_A")
        assert t2 is not None and t2 is not t1
        await asyncio.sleep(0.01)  # 让 t1 的迟到回调有机会捣乱
        assert ts._RENEWERS.get("train_A") is t2  # 仍登记新任务（NEW-6）
        ts.stop_renewal("train_A")

    try:
        asyncio.run(_run())
    finally:
        ts.stop_renewal("train_A")
