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

import asyncio
import json
from dataclasses import fields
from datetime import date, datetime, timedelta

import pytest

from backend.services.trade.services import decision_round_alerts as A
from backend.services.trade.services import decision_round_runner as RUNNER
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


# ── 停滞：worker 活着，但整天一轮都没跑成 ──────────────────────────────
# 四类 ``alert_round`` 告警的判据都吃 ``RoundResult``——**都以「跑过一轮」为前提**。
# 每个 tick 都被更早的闸门挡回（典型：交易日历不可用 → is_trading_day 抛）时一个
# RoundResult 都不产生，四种告警一条都不发；而 C07 判的是**进程心跳**，心跳写在循环
# 体顶部，进程活着它就新鲜。于是「面板全绿、整天没决策」。本组测的就是这一类的判据。
#
# 纪律（见 ``verification-vacuous-pass-guard`` 第九/十形态）：
# ①**时刻一律注入**（``now=``），绝不读墙上钟——否则白天绿、收盘后红；
# ②``_STALL_SEEN`` 是**模块级进程内去重集**，生产里一天一进程、测试里不清就会
#   前一个用例把后一个用例静默压掉，故 autouse fixture 前后各清一次。
LAST_HHMM = SLOTS[-1].hhmm  # 14:45
_DAY_STR = DAY.isoformat()


def _at_due(grace_min: int = 45) -> datetime:
    """过了最后槽位 + 宽限的时刻（缺省 15:30）——**注入**，不看墙上钟。"""
    last = SLOTS[-1]
    return last.due_at(DAY) + timedelta(minutes=grace_min)


def _entry(day: date = DAY, **over) -> str:
    payload = {"day": day.isoformat(), "status": STATUS_OK, "round_id": "rnd-x-1445"}
    payload.update(over)
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _clear_stall_seen():
    """``_STALL_SEEN`` 是模块级去重集：用例之间必须归零（见本组开头的纪律②）。"""
    RUNNER._STALL_SEEN.clear()
    yield
    RUNNER._STALL_SEEN.clear()


def test_before_the_last_slot_a_missing_round_is_normal() -> None:
    """还没到点：上午十点「今天还没有轮次」是**对的**，不许报。"""
    assert (
        A.stall_alert(
            now=datetime(2026, 9, 24, 10, 0, tzinfo=CST),
            raw_entries=[],
            expect_rounds=True,
            grace_min=45,
        )
        is None
    )


def test_the_grace_boundary_is_closed_on_the_due_side() -> None:
    """边界：差一分钟不报、到点就报（``>=``）——免得靠「差一点点」蒙混。"""
    assert not A.stall_due(
        _at_due() - timedelta(seconds=1), grace_min=45, last_slot=SLOTS[-1]
    )
    assert A.stall_due(_at_due(), grace_min=45, last_slot=SLOTS[-1])


def test_a_non_trading_day_never_reports_a_stall() -> None:
    """周末 worker 照样每 30s 醒一次，此时「没有轮次」是对的——推它等于每周误报两天。"""
    assert (
        A.stall_alert(
            now=_at_due(),
            raw_entries=[],
            expect_rounds=False,
            grace_min=45,
        )
        is None
    )


def test_a_day_with_a_round_is_not_a_stall() -> None:
    assert (
        A.stall_alert(
            now=_at_due(), raw_entries=[_entry()], expect_rounds=True, grace_min=45
        )
        is None
    )


def test_an_empty_log_is_a_stall_that_names_the_day() -> None:
    """空日志 ⇒ 报，且标题里必须有**日期**：值班要一眼看出停的是哪一天。"""
    alert = A.stall_alert(
        now=_at_due(), raw_entries=[], expect_rounds=True, grace_min=45
    )
    assert alert is not None
    assert alert.kind == A.ALERT_STALLED
    assert alert.level == "error"
    assert _DAY_STR in alert.title
    # 成因与两个入口都要在正文里：只说「没跑」等于让值班自己摸
    assert LAST_HHMM[:2] + ":" + LAST_HHMM[2:] in alert.content
    assert A.MANUAL_DIAGNOSE_HINT in alert.content
    assert A.MANUAL_RERUN_HINT in alert.content


def test_only_todays_round_counts() -> None:
    """log 保留 20 条、**跨日存活**：昨天的轮次不能把今天的停滞压下去。"""
    yesterday = date(2026, 9, 23)
    assert (
        A.stall_alert(
            now=_at_due(),
            raw_entries=[_entry(yesterday), _entry(yesterday)],
            expect_rounds=True,
            grace_min=45,
        )
        is not None
    )


def test_unreadable_entries_report_a_stall_rather_than_pass() -> None:
    """读不懂的条目**一律不算「跑过了」**：判据的失败方向必须是报出来。

    这一组正是「假通过」的温床——若实现写成「解析失败就 continue 并当没事」，
    日志被写坏的那天就会静默；写成本用例这样才与「宁可误报」一致。
    """
    junk: list[object] = [
        "{不合法 json",
        "3",
        "[1, 2]",
        '{"no_day": 1}',
        b"\xff\xfe",
        None,
        123,
    ]
    assert (
        A.stall_alert(now=_at_due(), raw_entries=junk, expect_rounds=True, grace_min=45)
        is not None
    )
    # 正向对照（防本用例变成「反正都报」的恒真断言）：合法且是今天的条目**不报**
    assert (
        A.stall_alert(
            now=_at_due(),
            raw_entries=[*junk, _entry()],
            expect_rounds=True,
            grace_min=45,
        )
        is None
    )


def test_the_calendar_note_reaches_the_reader() -> None:
    """日历不可用是**最常见的成因**，附注必须原样进正文（值班据此决定去不去看 C13）。"""
    alert = A.stall_alert(
        now=_at_due(),
        raw_entries=[],
        expect_rounds=True,
        grace_min=45,
        calendar_note="交易日历不可用（DateOutOfBounds），按「工作日」推断。",
    )
    assert alert is not None
    assert "DateOutOfBounds" in alert.content


@pytest.mark.asyncio
async def test_a_stall_is_pushed_once_even_when_the_dedupe_store_is_gone() -> None:
    """**Redis 全丢**时靠 ``seen`` 兜住重复：Redis 挂了既是常见成因，也让去重键写不下去。

    没有 ``seen`` 时这里会退化成每 ``POLL_S`` 一条（30 分钟推 60 条），把人训练成
    不看通知——而「不看通知」正好废掉整条可见性链。
    """
    notifier = SpyNotifier()
    kw = {
        "now": _at_due(),
        "raw_entries": [],
        "expect_rounds": True,
        "grace_min": 45,
        "user_id": "u1",
        "notifier": notifier,
        "redis": None,  # 去重装置整个没了
        "seen": set(),
    }
    assert await A.alert_stall(**kw)
    assert not await A.alert_stall(**kw)  # 第二次：本进程那把 seen 拦住
    assert len(notifier.calls) == 1


@pytest.mark.asyncio
async def test_the_stall_key_is_what_survives_a_restart() -> None:
    """跨重启那把是 Redis 键：换了进程（``seen`` 空）也不许再推一条。"""
    redis, notifier = FakeRedis(), SpyNotifier()
    kw = {
        "now": _at_due(),
        "raw_entries": [],
        "expect_rounds": True,
        "grace_min": 45,
        "user_id": "u1",
        "notifier": notifier,
        "redis": redis,
    }
    assert await A.alert_stall(**kw, seen=set())
    assert not await A.alert_stall(**kw, seen=set())  # 新进程：只认 Redis
    assert len(notifier.calls) == 1
    assert A.stall_key(DAY) in redis.store
    # 键形不带家段：停滞是账户级事实（见 stall_key 的 docstring）
    assert ":stalled" in A.stall_key(DAY) and "pro" not in A.stall_key(DAY)


@pytest.mark.asyncio
async def test_no_push_when_a_round_ran_but_the_same_pipe_still_pushes() -> None:
    """「有轮次就不推」必须配**正向对照**，否则断言 ``calls == []`` 恒真。

    两条路各用**全新的去重装置**：共用的话，「不推」可能只是被上一条写的去重键挡住，
    与判据毫无关系——那正是假通过。
    """
    notifier = SpyNotifier()
    not_ran = {
        "now": _at_due(),
        "raw_entries": [_entry()],
        "expect_rounds": True,
        "grace_min": 45,
        "user_id": "u1",
        "notifier": notifier,
        "redis": FakeRedis(),
        "seen": set(),
    }
    assert not await A.alert_stall(**not_ran)
    assert notifier.calls == []
    # 正向对照：同一条管路、只把日志换成空的 ⇒ 推得出去（证明上一步的沉默来自判据）
    assert await A.alert_stall(
        now=_at_due(),
        raw_entries=[],
        expect_rounds=True,
        grace_min=45,
        user_id="u1",
        notifier=notifier,
        redis=FakeRedis(),
        seen=set(),
    )
    assert len(notifier.calls) == 1


# ── 驱动层：谁在什么时候问这个问题 ────────────────────────────────────
# 判据在 ``alerts``、取数在 ``runner``。这一层测两件事：**日历三态怎么收口**、以及
# **循环真的每周期都问**（后者必须是行为测试——删掉循环里那行调用要能变红，
# 字符串断言证明不了「跑得起来」，见 ``verification-vacuous-pass-guard`` 第四形态）。
class _VerdictService:
    """``TradingCalendarService`` 替身：只铺本路径会碰的那一个方法。"""

    def __init__(self, verdict=None, source=None, raises=None) -> None:
        self._verdict, self._source, self._raises = verdict, source, raises

    async def trading_day_verdict(self, **_kw):
        if self._raises is not None:
            raise self._raises
        return self._verdict, self._source


def _patch_calendar(monkeypatch, service) -> None:
    import backend.shared.trading_calendar as cal

    monkeypatch.setattr(cal, "TradingCalendarService", lambda: service)


WEEKDAY = date(2026, 9, 24)  # 周四
SATURDAY = date(2026, 9, 26)


@pytest.mark.asyncio
async def test_the_authoritative_verdict_is_used_as_is(monkeypatch) -> None:
    _patch_calendar(monkeypatch, _VerdictService(True, "exchange_calendar"))
    assert await RUNNER._expect_rounds_today(WEEKDAY) == (True, "")


@pytest.mark.asyncio
async def test_a_holiday_from_the_authoritative_calendar_is_respected(
    monkeypatch,
) -> None:
    """权威日历说「休市」就得安静：国庆/中秋不是交易日，本来就不该有轮次。"""
    _patch_calendar(monkeypatch, _VerdictService(False, "exchange_calendar"))
    assert await RUNNER._expect_rounds_today(WEEKDAY) == (False, "")


@pytest.mark.asyncio
async def test_a_degraded_calendar_on_a_weekday_is_treated_as_expecting_rounds(
    monkeypatch,
) -> None:
    """**降级的三态收口**：日历读不到时工作日按「该出」办——正是这种日子决策层停摆
    （每个 tick 都被 fail-closed 拒跑），最需要有人知道；且附注必须带上成因。"""
    _patch_calendar(monkeypatch, _VerdictService(True, "weekday_fallback"))
    expect, note = await RUNNER._expect_rounds_today(WEEKDAY)
    assert expect is True
    assert "降级" in note


@pytest.mark.asyncio
async def test_a_degraded_calendar_on_the_weekend_stays_quiet(monkeypatch) -> None:
    """第二半同样重要：周末 + 降级**不能**报——否则每周误报两天，人就把它静音了。"""
    _patch_calendar(monkeypatch, _VerdictService(True, "weekday_fallback"))
    expect, note = await RUNNER._expect_rounds_today(SATURDAY)
    assert expect is False
    assert note  # 成因照留（日志里有它，排查看得见）


@pytest.mark.asyncio
async def test_a_calendar_that_raises_does_not_escape(monkeypatch) -> None:
    """抛异常与降级同办：都是「日历这层不可用」。"""
    _patch_calendar(
        monkeypatch, _VerdictService(raises=RuntimeError("DateOutOfBounds"))
    )
    expect, note = await RUNNER._expect_rounds_today(WEEKDAY)
    assert expect is True
    assert "DateOutOfBounds" in note


class _LogRedis:
    """只铺停滞检查要用的两个动作（``lrange`` 读日志；去重键走 set/get）。"""

    def __init__(self, entries: list[str]) -> None:
        self.entries = entries
        self.store: dict[str, str] = {}
        self.lrange_calls: list[tuple] = []

    def lrange(self, key, start, end):
        self.lrange_calls.append((key, start, end))
        return list(self.entries)

    def set(self, key, value, nx=False, ex=None):  # noqa: A002 - 与 redis-py 同形
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)


def _patch_stall_env(monkeypatch, *, redis, now, service) -> SpyNotifier:
    """把 ``_stall_watch`` 的三个外界分别接上：时钟、Redis、日历、通知器。"""
    import backend.shared.decision_context_source as ctx

    monkeypatch.setattr(ctx, "now_cn", lambda: now)
    monkeypatch.setattr(RUNNER, "native_redis_client", lambda: redis, raising=False)
    import backend.services.trade.services.decision_round_io as IO

    monkeypatch.setattr(IO, "native_redis_client", lambda: redis)
    _patch_calendar(monkeypatch, service)
    notifier = SpyNotifier()
    monkeypatch.setattr(A, "default_notifier", lambda: notifier)
    return notifier


@pytest.mark.asyncio
async def test_stall_watch_reads_the_log_and_pushes_on_a_dead_day(monkeypatch) -> None:
    redis = _LogRedis([])
    notifier = _patch_stall_env(
        monkeypatch,
        redis=redis,
        now=_at_due(),
        service=_VerdictService(True, "exchange_calendar"),
    )
    await RUNNER._stall_watch()
    assert redis.lrange_calls, "必须真去读当天日志（否则判据吃的是空集）"
    assert len(notifier.calls) == 1


@pytest.mark.asyncio
async def test_stall_watch_is_silent_before_it_is_due(monkeypatch) -> None:
    """未到点就不该问 Redis/日历——worker 每 30s 一次，绝大多数 tick 的答案都是这个。

    正向对照同上：同一套替身、只把时刻推到到点，必须推得出去。
    """
    early = datetime(2026, 9, 24, 10, 0, tzinfo=CST)
    redis = _LogRedis([])
    early_notifier = _patch_stall_env(
        monkeypatch,
        redis=redis,
        now=early,
        service=_VerdictService(True, "exchange_calendar"),
    )
    await RUNNER._stall_watch()
    assert redis.lrange_calls == []
    assert early_notifier.calls == []

    # 正向对照：同一条管路、只把时刻推到到点 ⇒ 推得出去（证明上一步的沉默来自时刻闸门）
    due_notifier = _patch_stall_env(
        monkeypatch,
        redis=redis,
        now=_at_due(),
        service=_VerdictService(True, "exchange_calendar"),
    )
    await RUNNER._stall_watch()
    assert len(due_notifier.calls) == 1


@pytest.mark.asyncio
async def test_stall_watch_is_silent_when_todays_round_is_in_the_log(
    monkeypatch,
) -> None:
    redis = _LogRedis([_entry()])
    notifier = _patch_stall_env(
        monkeypatch,
        redis=redis,
        now=_at_due(),
        service=_VerdictService(True, "exchange_calendar"),
    )
    await RUNNER._stall_watch()
    assert redis.lrange_calls
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_stall_watch_never_raises_when_redis_is_down(monkeypatch) -> None:
    """Redis 挂了是「整天没跑成」的常见成因：此时既读不到日志也推不出去，
    但**调用点不许因此炸**（它跑在刚结束一轮的循环里）。"""

    def boom():
        raise RuntimeError("redis 全挂了")

    monkeypatch.setattr(RUNNER, "native_redis_client", boom, raising=False)
    import backend.services.trade.services.decision_round_io as IO

    monkeypatch.setattr(IO, "native_redis_client", boom)
    import backend.shared.decision_context_source as ctx

    monkeypatch.setattr(ctx, "now_cn", lambda: _at_due())
    _patch_calendar(monkeypatch, _VerdictService(True, "exchange_calendar"))
    notifier = SpyNotifier()
    monkeypatch.setattr(A, "default_notifier", lambda: notifier)

    await RUNNER._stall_watch()  # 不抛即通过
    assert len(notifier.calls) == 1  # 读不到日志 ⇒ 按「没跑过」报（fail-loud）


@pytest.mark.asyncio
async def test_the_worker_loop_asks_the_stall_question_every_cycle(monkeypatch) -> None:
    """**行为测试**：循环体里那行调用被删掉时本用例必须变红。

    ``round_tick`` 换成「永远空手而归」（正是停摆那天的样子），``_stall_watch`` 换成
    记账替身；``_poll_s`` 归零让循环转得起来，跑几个周期后取消。
    """
    import backend.shared.env_flags as flags

    monkeypatch.setattr(flags, "env_flag", lambda *_a, **_k: True)
    monkeypatch.setattr(RUNNER, "_poll_s", lambda: 0)
    monkeypatch.setattr(RUNNER, "_grace_min", lambda: 45)

    async def empty_tick(**_kw):
        return ()

    monkeypatch.setattr(RUNNER, "round_tick", empty_tick)
    seen_calls: list[int] = []

    async def spy_watch() -> None:
        seen_calls.append(1)

    monkeypatch.setattr(RUNNER, "_stall_watch", spy_watch)
    import backend.shared.scheduler_registry as sched

    monkeypatch.setattr(sched, "heartbeat", lambda *_a, **_k: None)

    task = asyncio.create_task(RUNNER.run_decision_round_worker())
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen_calls, "循环每周期都必须问一次停滞问题（空 tick 时更必须问）"
