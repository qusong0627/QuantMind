"""P4 附-②：决策轮失败可见性（哪一轮要推人、推什么、一天推几次）。

为什么单独一个模块测：一轮里的「单没出去」有三个落点——审计行（要不要去看）、
状态键（知不知道该看）与**推送**（不用看就知道）。前两个决策轮已有，第三个此前
没有，而状态键**一个读者也没有**（无 API、无面板，只有 ``scripts/decision_ledger.py``
这种手动入口）。于是「计划要卖、单没出去」这条完整链条在系统里的唯一痕迹是审计表
里的一列——要人到场才看得见，而人不在场正是需要它的场合。

替身策略：判据（哪些结果要推、推什么文案）走**纯函数**真件；去重走字典客户端
替身（``SET NX`` 语义与 redis-py 同形）；接线测试把 ``run_once`` 换成罐头结果——
那一层测的是「tick 拿到结果后做了什么」，真跑一轮是 ``test_decision_round.py`` 的事。
"""

from __future__ import annotations

from dataclasses import fields
from datetime import date, datetime

import pytest

from backend.services.trade.services import decision_round_alerts as A
from backend.services.trade.services import decision_round_tick as TICK
from backend.services.trade.services.decision_round_core import (
    CST,
    SLOTS,
    STATUS_ABORTED,
    STATUS_ERROR,
    STATUS_LLM_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    AgentRun,
    RoundDeps,
    RoundResult,
)

DAY = date(2026, 9, 24)
SLOT_REBALANCE = next(s for s in SLOTS if s.hhmm == "0935")


def _result(**over) -> RoundResult:
    base: dict = {
        "status": STATUS_OK,
        "day": DAY,
        "slot": SLOT_REBALANCE,
        "round_id": "rnd-20260924-0935",
        "agent": "pro",
        "decisions": 3,
    }
    base.update(over)
    return RoundResult(**base)


# ── 判据：哪些结果要推 ────────────────────────────────────────────────
def test_a_clean_round_has_nothing_to_say() -> None:
    """提交完成的一轮不推——推送只留给「本来说要做、结果没做」。"""
    assert A.round_alert(_result(submitted=2, legs=2)) is None


def test_a_skipped_round_has_nothing_to_say() -> None:
    """「按设计跳过」不是失败：补跑槽看到当日已有决策就是跳过，推它等于天天误报。"""
    assert A.round_alert(_result(status=STATUS_SKIPPED, submitted=0, legs=0)) is None


def test_aborted_round_says_nothing_was_sent_and_where_the_retry_is() -> None:
    """aborted 的语义是**一张单都没发**（在途账读不到）——要推，且要说清怎么补。"""
    alert = A.round_alert(
        _result(
            status=STATUS_ABORTED,
            legs=2,
            submitted=0,
            note="在途委托不可信：本轮不下单（fail-closed）— sim 读超时",
            errors=("在途委托不可信：本轮不下单（fail-closed）— sim 读超时",),
        )
    )
    assert alert is not None
    assert alert.kind == A.ALERT_ABORTED
    assert alert.level == "error"
    assert "一张单都没发" in alert.title
    # 原因必须原样带上：只说「中止」等于让值班去猜是哪一层断了
    assert "sim 读超时" in alert.content
    # 补跑槽会自动重来这件事要写出来，否则运营会去手动 --force（那是真的再下一轮单）
    assert "补跑" in alert.content
    assert "2" in alert.content  # 计划了几条腿


def test_failed_legs_are_reported_with_the_count_and_the_reason() -> None:
    """腿发出去被拒 = 意图丢了：这是最需要人知道的一类，且**不会**自动重发。"""
    alert = A.round_alert(
        _result(
            legs=3,
            submitted=1,
            failed=2,
            round_id="rnd-20260924-0935",
        )
    )
    assert alert is not None
    assert alert.kind == A.ALERT_FAILED
    assert alert.level == "error"
    assert "2" in alert.title
    assert "rnd-20260924-0935" in alert.content
    # 说清「不会自动重发」，否则值班以为系统会自己再试
    assert "不会自动重发" in alert.content


def test_llm_failure_is_reported_under_its_own_kind() -> None:
    """模型一家跑不动是**运维问题**（欠费/改配置），与「单没出去」不同类，分开计数。"""
    alert = A.round_alert(
        _result(status=STATUS_LLM_FAILED, decisions=0, note="RuntimeError: 401 欠费")
    )
    assert alert is not None
    assert alert.kind == A.ALERT_LLM_FAILED
    assert alert.level == "error"
    assert "401 欠费" in alert.content


def test_execution_error_status_is_reported() -> None:
    """执行段落账炸了（error）时腿可能**已经真出去了**——文案不许说「没发单」。"""
    alert = A.round_alert(
        _result(status=STATUS_ERROR, note="执行段失败：ConnectionError: db down")
    )
    assert alert is not None
    assert alert.kind == A.ALERT_ERROR
    assert "ConnectionError" in alert.content


def test_failed_legs_are_reported_even_though_the_round_is_ok() -> None:
    """status=ok 但 failed>0 是最容易被漏掉的一类：状态键写着「跑完了」，而单没出去。"""
    alert = A.round_alert(_result(status=STATUS_OK, legs=2, submitted=0, failed=2))
    assert alert is not None and alert.kind == A.ALERT_FAILED


def test_abort_with_orders_already_out_is_not_alerted() -> None:
    """已提交过腿的轮次不算 aborted（同 ``decision_round`` 的组合规则）：一条都没发才推。"""
    assert A.round_alert(_result(status=STATUS_OK, legs=2, submitted=1)) is None


# ── 去重与送达 ────────────────────────────────────────────────────────
class FakeRedis:
    """``SET NX EX`` 语义的字典替身（与 redis-py 同形）。"""

    def __init__(self, fail_set: bool = False, fail_get: bool = False) -> None:
        self.store: dict[str, object] = {}
        self.fail_set = fail_set
        self.fail_get = fail_get

    def set(self, key, value, nx=False, ex=None):  # noqa: A002 - 与 redis-py 同形
        if self.fail_set:
            raise RuntimeError("redis 写不下来")
        if nx and key in self.store:
            return None
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


@pytest.mark.asyncio
async def test_the_same_kind_is_pushed_once_a_day_per_agent() -> None:
    """同一家、同一类失败一天只推一次：值班看的是「今天有这类失败」，次数在状态键里。"""
    redis, notifier = FakeRedis(), SpyNotifier()
    result = _result(status=STATUS_ABORTED, legs=2, note="在途账读不到")
    assert await A.alert_round(result, notifier=notifier, redis=redis, user_id="u1")
    assert not await A.alert_round(result, notifier=notifier, redis=redis, user_id="u1")
    assert len(notifier.calls) == 1


def test_two_agents_are_pushed_separately() -> None:
    """名册下每家各推各的：一家 aborted 不许把另一家的那条吃掉。"""
    redis = FakeRedis()
    assert A.alert_key(_result(agent="pro"), A.ALERT_ABORTED) != A.alert_key(
        _result(agent="mini"), A.ALERT_ABORTED
    )
    assert "pro" in A.alert_key(_result(agent="pro"), A.ALERT_ABORTED)
    assert redis  # 键形断言在上面（本用例只钉「按家分段」）


@pytest.mark.asyncio
async def test_the_dedupe_key_is_written_only_after_delivery() -> None:
    """送达失败**不许**烧掉去重键：那条告警还没被人看到，下一轮必须再试。"""
    redis = FakeRedis()
    bad = SpyNotifier(ok=False)
    result = _result(status=STATUS_ABORTED, note="在途账读不到")
    assert not await A.alert_round(result, notifier=bad, redis=redis, user_id="u1")
    assert redis.store == {}  # 没送达 ⇒ 没记键

    good = SpyNotifier()
    assert await A.alert_round(result, notifier=good, redis=redis, user_id="u1")
    assert len(good.calls) == 1
    assert redis.store  # 送达之后才记


@pytest.mark.asyncio
async def test_a_notifier_exception_never_escapes() -> None:
    """通知服务炸了不许带走刚跑完的一轮（此刻单可能已经真出去了）。"""
    redis = FakeRedis()
    ok = await A.alert_round(
        _result(status=STATUS_ABORTED, note="x"),
        notifier=SpyNotifier(raises=True),
        redis=redis,
        user_id="u1",
    )
    assert ok is False
    assert redis.store == {}


@pytest.mark.asyncio
async def test_without_redis_it_still_delivers_and_never_crashes() -> None:
    """读不到去重装置时**宁可重复推**：少推一条比多推一条的代价大得多。"""
    notifier = SpyNotifier()
    assert await A.alert_round(
        _result(status=STATUS_ABORTED, note="x"),
        notifier=notifier,
        redis=None,
        user_id="u1",
    )
    assert len(notifier.calls) == 1
    # 第二次依旧推（无去重装置可记）
    assert await A.alert_round(
        _result(status=STATUS_ABORTED, note="x"),
        notifier=notifier,
        redis=None,
        user_id="u1",
    )
    assert len(notifier.calls) == 2


@pytest.mark.asyncio
async def test_a_broken_redis_does_not_swallow_the_alert() -> None:
    """去重装置坏了也一样：键写不下去 ⇒ 推出去（重复优于沉默）。"""
    notifier = SpyNotifier()
    assert await A.alert_round(
        _result(status=STATUS_ABORTED, note="x"),
        notifier=notifier,
        redis=FakeRedis(fail_set=True),
        user_id="u1",
    )
    assert len(notifier.calls) == 1


@pytest.mark.asyncio
async def test_a_clean_round_never_touches_the_notification_machinery(
    monkeypatch,
) -> None:
    """正常的一轮跑一万次也不该在通知链路上留足迹：连生产通知器都不许被现造。

    这条不只是洁癖——``default_notifier`` 会连库。真跑起来的 tick 若在**没事**的轮次
    也现造它，等于每轮都摸一次通知设施；测试里更是会把「无事的轮次」变成一次真写库。
    """

    def boom() -> A.Notifier:
        raise AssertionError("无事的轮次不该现造通知器")

    monkeypatch.setattr(A, "default_notifier", boom)
    assert not await A.alert_round(
        _result(submitted=1, legs=1), notifier=None, redis=FakeRedis(), user_id="u1"
    )


@pytest.mark.asyncio
async def test_a_clean_round_never_calls_the_notifier() -> None:
    notifier = SpyNotifier()
    assert not await A.alert_round(
        _result(submitted=1, legs=1), notifier=notifier, redis=FakeRedis(), user_id="u1"
    )
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_a_missing_user_id_is_not_sent_to_nobody() -> None:
    """账户坐标取不到时**不推**（推给空 user 的通知落在谁那里都不对），但要留日志。"""
    notifier = SpyNotifier()
    assert not await A.alert_round(
        _result(status=STATUS_ABORTED, note="x"),
        notifier=notifier,
        redis=FakeRedis(),
        user_id="",
    )
    assert notifier.calls == []


# ── 接线：tick 拿到一轮结果之后 ────────────────────────────────────────
def _bare_deps(**over) -> RoundDeps:
    """只铺 tick 在这条路径上真正会碰的字段（其余置 None，碰到即 AttributeError）."""
    values = {f.name: None for f in fields(RoundDeps)}
    values.update(
        roster=lambda: (),
        is_trading_day=None,  # 由用例覆盖
        account_user=lambda: "00000001",
    )
    values.update(over)
    return RoundDeps(**values)


async def _trading_day(_day) -> bool:
    return True


class FakeNative:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}
        self.calls: list[tuple] = []

    def set(self, key, value, nx=False, ex=None):  # noqa: A002 - 与 redis-py 同形
        self.calls.append(("set", key, nx))
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.calls.append(("delete", key))
        return 1

    def lpush(self, key, value):
        self.calls.append(("lpush", key))
        return 1

    def ltrim(self, key, start, end):
        return True

    def close(self):
        return None


@pytest.mark.asyncio
async def test_tick_pushes_when_the_round_aborts(monkeypatch) -> None:
    """接线判据：``run_once`` 吐 aborted ⇒ 通知出去，且带的是**这一轮真跑的**那家。"""

    async def fake_run_once(slot, *, deps, now, day):
        return RoundResult(
            status=STATUS_ABORTED,
            day=day,
            slot=slot,
            round_id="rnd-x-0935",
            agent="",  # 绑定取不出来时 run_once 认不出家，由 tick 补
            note="在途委托不可信：本轮不下单（fail-closed）",
            legs=2,
        )

    monkeypatch.setattr(TICK, "run_once", fake_run_once)
    runs = (AgentRun(agent="pro", load_llm=lambda: None),)
    deps = _bare_deps(
        is_trading_day=_trading_day, roster=lambda: runs, now=lambda: None
    )
    native, notifier = FakeNative(), SpyNotifier()
    out = await TICK.round_tick(
        deps=deps,
        native=native,
        grace_min=0,  # 只让 09:35 到点：09:00 还在宽限窗里（那会让一轮变两轮）
        now=datetime(2026, 9, 24, 9, 35, tzinfo=CST),
        notify=notifier,
    )
    assert len(out) == 1
    assert len(notifier.calls) == 1
    user_id, title, content, level = notifier.calls[0]
    assert user_id == "00000001"
    assert level == "error"
    # 家段在**标题**里（列表视图只显示标题）：没有它，值班查不出是哪家在报警
    assert "pro" in title


@pytest.mark.asyncio
async def test_tick_does_not_push_on_a_clean_round(monkeypatch) -> None:
    async def fake_run_once(slot, *, deps, now, day):
        return RoundResult(
            status=STATUS_OK,
            day=day,
            slot=slot,
            round_id="rnd-x-0935",
            submitted=2,
            legs=2,
        )

    monkeypatch.setattr(TICK, "run_once", fake_run_once)
    deps = _bare_deps(is_trading_day=_trading_day, now=lambda: None)
    notifier = SpyNotifier()

    await TICK.round_tick(
        deps=deps,
        native=FakeNative(),
        grace_min=0,  # 只让 09:35 到点：09:00 还在宽限窗里（那会让一轮变两轮）
        now=datetime(2026, 9, 24, 9, 35, tzinfo=CST),
        notify=notifier,
    )
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_a_broken_account_lookup_does_not_break_the_round(monkeypatch) -> None:
    """账户坐标取不到 ⇒ 不推，但**done 键与状态键照写**：一轮真跑完的结果不能被通知层吃掉。"""

    async def fake_run_once(slot, *, deps, now, day):
        return RoundResult(
            status=STATUS_ABORTED, day=day, slot=slot, round_id="rnd-x-0935", legs=1
        )

    def boom():
        raise RuntimeError("账户解析炸了")

    monkeypatch.setattr(TICK, "run_once", fake_run_once)
    deps = _bare_deps(is_trading_day=_trading_day, account_user=boom, now=lambda: None)
    native, notifier = FakeNative(), SpyNotifier()

    out = await TICK.round_tick(
        deps=deps,
        native=native,
        grace_min=0,  # 只让 09:35 到点：09:00 还在宽限窗里（那会让一轮变两轮）
        now=datetime(2026, 9, 24, 9, 35, tzinfo=CST),
        notify=notifier,
    )
    assert len(out) == 1 and out[0].status == STATUS_ABORTED
    assert notifier.calls == []
    assert any(c[0] == "set" and c[2] is False for c in native.calls)  # 状态键写了
