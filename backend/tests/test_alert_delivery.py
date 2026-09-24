"""告警投递引擎（``shared.alert_delivery``）：一条纪律，多处消费。

本模块测的是**纪律本身**，不是某一条腿的判据（判据见
``services/tests/test_tdx_exec_alerts.py`` 与 ``test_decision_round_alerts.py``）。
抽出来的理由也在这：这套纪律的每一句都是踩出来的——先记键后推送 ⇒ 推失败的那条
告警永远不会再试；读键失败就算「推过了」⇒ 去重设施一挂就把故障一起静音；
通知层抛异常带走调用点 ⇒ 一轮已经成交的交易被变成异常。

替身策略：Redis 用字典替身（``set/get`` 与 redis-py 同形，可注入读/写失败）；
通知器记调用、可注入「返回 False」与「抛异常」两种失败形态。
"""

from __future__ import annotations

import pytest

from backend.shared.alert_delivery import (
    ALERT_TTL_S,
    Alert,
    alert_sent,
    deliver_alert,
    remember_sent,
)

KEY = "qm:test:alert:2026-09-24:l2:positions_unreadable"


class FakeRedis:
    """内存替身：``set/get`` 与 redis-py 同形，可注入读/写失败。"""

    def __init__(self, fail_set: bool = False, fail_get: bool = False) -> None:
        self.store: dict[str, object] = {}
        self.fail_set = fail_set
        self.fail_get = fail_get
        self.set_calls: list[tuple[str, object, object]] = []

    def set(self, key, value, ex=None):
        self.set_calls.append((key, value, ex))
        if self.fail_set:
            raise RuntimeError("redis 写不下来")
        self.store[key] = value
        return True

    def get(self, key):
        if self.fail_get:
            raise RuntimeError("redis 读不下来")
        return self.store.get(key)


class SpyNotifier:
    def __init__(self, ok: bool = True, raises: bool = False) -> None:
        self.ok = ok
        self.raises = raises
        self.calls: list[tuple] = []

    async def __call__(self, user_id, title, content, level="info") -> bool:
        self.calls.append((user_id, title, content, level))
        if self.raises:
            raise RuntimeError("通知服务炸了")
        return self.ok


def _alert(kind: str = "positions_unreadable") -> Alert:
    return Alert(
        kind=kind,
        level="error",
        title="滚动买卖 本轮 3 条信号跳过：持仓读不到",
        content="通达信桥未连接",
    )


# ── 判据函数（纯函数，无 IO） ──────────────────────────────────────────
def test_a_missing_redis_counts_as_never_sent() -> None:
    """没有去重装置 ⇒ 一切照推（重复优于沉默）。"""
    assert alert_sent(None, KEY) is False


def test_a_broken_redis_read_counts_as_never_sent() -> None:
    """读不到键按「没推过」办：去重是为了少打扰，不是为了保证沉默。"""
    assert alert_sent(FakeRedis(fail_get=True), KEY) is False


def test_remember_sent_uses_the_day_scale_ttl() -> None:
    """去重口径是「一天一次」，TTL 必须跨过一整个交易日。"""
    redis = FakeRedis()
    remember_sent(redis, KEY)
    assert redis.store[KEY] == "1"
    assert redis.set_calls[0][2] == ALERT_TTL_S
    assert ALERT_TTL_S > 24 * 3600


def test_remember_sent_never_raises_on_a_broken_redis() -> None:
    """记键失败只意味着下次重复推一条——不是要拦下的错，更不许抛给调用方。"""
    remember_sent(FakeRedis(fail_set=True), KEY)  # 不抛即通过


# ── 投递 ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_dedupe_key_is_written_only_after_delivery() -> None:
    """送达失败**不许**烧掉去重键：那条告警还没被人看到，下一轮必须再试。"""
    redis = FakeRedis()
    bad = SpyNotifier(ok=False)
    assert not await deliver_alert(
        _alert(), key=KEY, user_id="u1", redis=redis, notifier_factory=lambda: bad
    )
    assert redis.store == {}  # 没送达 ⇒ 没记键

    good = SpyNotifier()
    assert await deliver_alert(
        _alert(), key=KEY, user_id="u1", redis=redis, notifier_factory=lambda: good
    )
    assert len(good.calls) == 1
    assert redis.store[KEY] == "1"


@pytest.mark.asyncio
async def test_the_production_wrapper_client_also_dedupes() -> None:
    """**包装层**（``trade_shared.redis_client.RedisClient``）当去重设施也必须真去重。

    它的 ``set`` 是 ``(key, value, ttl=)`` 而不是 ``ex=``，且把异常吞成一行日志：
    只认 ``ex=`` 的写法在这里会 TypeError → 被吞 → 键写不下去 → **去重静默失效**
    （L2 循环每周期重推一条）。P2.6 记下的同一个坑，故用真件而不是自造替身钉。
    """
    from backend.services.trade_shared.redis_client import RedisClient

    wrapper = RedisClient()
    wrapper.client = FakeRedis()
    notifier = SpyNotifier()
    assert await deliver_alert(
        _alert(),
        key=KEY,
        user_id="u1",
        redis=wrapper,
        notifier_factory=lambda: notifier,
    )
    assert KEY in wrapper.client.store, "包装层上去重键没写下去——去重会静默失效"
    assert not await deliver_alert(
        _alert(),
        key=KEY,
        user_id="u1",
        redis=wrapper,
        notifier_factory=lambda: notifier,
    )
    assert len(notifier.calls) == 1


@pytest.mark.asyncio
async def test_the_wrapper_without_a_connection_does_not_crash_the_alert() -> None:
    """包装层没连上（``.client is None``）时：读按"没推过"办，写静默失败，不抛。"""
    from backend.services.trade_shared.redis_client import RedisClient

    wrapper = RedisClient()  # 从未 connect() ⇒ .client is None
    notifier = SpyNotifier()
    assert await deliver_alert(
        _alert(),
        key=KEY,
        user_id="u1",
        redis=wrapper,
        notifier_factory=lambda: notifier,
    )
    assert len(notifier.calls) == 1  # 重复优于沉默


@pytest.mark.asyncio
async def test_an_already_sent_key_is_not_pushed_again() -> None:
    redis = FakeRedis()
    notifier = SpyNotifier()
    assert await deliver_alert(
        _alert(), key=KEY, user_id="u1", redis=redis, notifier_factory=lambda: notifier
    )
    assert not await deliver_alert(
        _alert(), key=KEY, user_id="u1", redis=redis, notifier_factory=lambda: notifier
    )
    assert len(notifier.calls) == 1


@pytest.mark.asyncio
async def test_a_broken_redis_does_not_swallow_the_alert() -> None:
    """去重装置坏了也一样：键写不下去 ⇒ 推出去。"""
    notifier = SpyNotifier()
    assert await deliver_alert(
        _alert(),
        key=KEY,
        user_id="u1",
        redis=FakeRedis(fail_set=True),
        notifier_factory=lambda: notifier,
    )
    assert len(notifier.calls) == 1


@pytest.mark.asyncio
async def test_a_broken_redis_read_does_not_silence_the_alert() -> None:
    notifier = SpyNotifier()
    assert await deliver_alert(
        _alert(),
        key=KEY,
        user_id="u1",
        redis=FakeRedis(fail_get=True),
        notifier_factory=lambda: notifier,
    )
    assert len(notifier.calls) == 1


@pytest.mark.asyncio
async def test_without_redis_it_still_delivers_every_time() -> None:
    """``redis=None`` ⇒ 没有去重设施（测试路径），一切照推。"""
    notifier = SpyNotifier()
    factory = lambda: notifier  # noqa: E731 — 断言「工厂每次都给同一个替身」
    for _ in range(2):
        assert await deliver_alert(
            _alert(), key=KEY, user_id="u1", redis=None, notifier_factory=factory
        )
    assert len(notifier.calls) == 2


@pytest.mark.asyncio
async def test_a_notifier_exception_never_escapes() -> None:
    """通知服务炸了不许带走调用点（此刻可能已经有真单出去了）。"""
    redis = FakeRedis()
    ok = await deliver_alert(
        _alert(),
        key=KEY,
        user_id="u1",
        redis=redis,
        notifier_factory=lambda: SpyNotifier(raises=True),
    )
    assert ok is False
    assert redis.store == {}


@pytest.mark.asyncio
async def test_a_missing_account_coordinate_is_not_sent_to_nobody() -> None:
    """账户坐标取不到时**不推**（推给空 user 的通知落在谁那里都不对），但不记键。"""
    redis = FakeRedis()
    notifier = SpyNotifier()
    for uid in ("", None, "  "):
        assert not await deliver_alert(
            _alert(),
            key=KEY,
            user_id=uid,
            redis=redis,
            notifier_factory=lambda: notifier,
        )
    assert notifier.calls == []
    assert redis.store == {}


@pytest.mark.asyncio
async def test_no_notifier_factory_means_no_push_and_no_crash() -> None:
    """``notifier_factory=None`` 是调用方的显式选择，不是异常：返回 False 并留日志。"""
    assert not await deliver_alert(_alert(), key=KEY, user_id="u1", redis=FakeRedis())


@pytest.mark.asyncio
async def test_a_deduped_alert_never_touches_the_notifier_factory() -> None:
    """已经推过 ⇒ 连通知器都不许现造（正常路径在通知链路上零足迹）。"""

    def boom():
        raise AssertionError("已推过的告警不该再碰通知设施")

    redis = FakeRedis()  # 假装今天已经推过
    remember_sent(redis, KEY)
    assert not await deliver_alert(
        _alert(), key=KEY, user_id="u1", redis=redis, notifier_factory=boom
    )


# ── 进程内去重集（循环型调用方） ───────────────────────────────────────
@pytest.mark.asyncio
async def test_seen_stops_a_loop_from_pushing_every_cycle() -> None:
    """Redis 读不出来时，只靠跨进程去重键会退化成**每周期推一条**（60s 一条）。

    这不是「重复优于沉默」——那是把值班训练成不看通知。``seen`` 记本进程的账。
    """
    notifier = SpyNotifier()
    seen: set[str] = set()
    redis = FakeRedis(fail_get=True)

    for _ in range(5):  # 五个周期的循环
        await deliver_alert(
            _alert(),
            key=KEY,
            user_id="u1",
            redis=redis,
            notifier_factory=lambda: notifier,
            seen=seen,
        )
    assert len(notifier.calls) == 1


@pytest.mark.asyncio
async def test_without_seen_the_same_loop_would_repeat() -> None:
    """对照组：不传 ``seen`` 时同样的循环推了 5 条——差异只来自去重集。"""
    notifier = SpyNotifier()
    redis = FakeRedis(fail_get=True)
    for _ in range(5):
        await deliver_alert(
            _alert(),
            key=KEY,
            user_id="u1",
            redis=redis,
            notifier_factory=lambda: notifier,
        )
    assert len(notifier.calls) == 5


@pytest.mark.asyncio
async def test_seen_learns_from_the_redis_key_so_a_later_read_failure_is_harmless() -> (
    None
):
    """Redis 里已有 ⇒ 本进程也记上：此后 Redis 短暂读不到也不至于重复推。"""
    redis = FakeRedis()
    remember_sent(redis, KEY)
    seen: set[str] = set()
    notifier = SpyNotifier()
    assert not await deliver_alert(
        _alert(),
        key=KEY,
        user_id="u1",
        redis=redis,
        notifier_factory=lambda: notifier,
        seen=seen,
    )
    assert seen == {KEY}

    redis.fail_get = True  # Redis 随后读不出来了
    assert not await deliver_alert(
        _alert(),
        key=KEY,
        user_id="u1",
        redis=redis,
        notifier_factory=lambda: notifier,
        seen=seen,
    )
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_seen_is_keyed_so_a_new_kind_still_goes_out() -> None:
    """``seen`` 按**键**去重：同一进程里换一类失败（或换一天）照推。"""
    notifier = SpyNotifier()
    seen: set[str] = set()
    redis = FakeRedis(fail_get=True)
    other = "qm:test:alert:2026-09-24:l2:cycle_error"
    await deliver_alert(
        _alert(),
        key=KEY,
        user_id="u1",
        redis=redis,
        notifier_factory=lambda: notifier,
        seen=seen,
    )
    await deliver_alert(
        _alert("cycle_error"),
        key=other,
        user_id="u1",
        redis=redis,
        notifier_factory=lambda: notifier,
        seen=seen,
    )
    assert len(notifier.calls) == 2


@pytest.mark.asyncio
async def test_the_notifier_gets_the_account_coordinate_and_the_level() -> None:
    """通知器形状契约：``(user_id, title, content, level)``，账户坐标由构造方闭合。"""
    notifier = SpyNotifier()
    await deliver_alert(
        _alert(),
        key=KEY,
        user_id=10000001,
        redis=FakeRedis(),
        notifier_factory=lambda: notifier,
    )
    uid, title, content, level = notifier.calls[0]
    assert uid == "10000001"  # 数字坐标也要变成字符串（通知表按字符串存）
    assert title == "滚动买卖 本轮 3 条信号跳过：持仓读不到"
    assert content == "通达信桥未连接"
    assert level == "error"
