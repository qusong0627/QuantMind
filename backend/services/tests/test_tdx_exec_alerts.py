"""TDX 执行腿失败可见性（``tdx_exec_alerts``）：判据 + 投递。

**为什么单独一个模块测**：这两条腿（滚动买卖、L2 实时）的失败此前只落在
``logger.warning`` 上。判据有、单测有、日志也在写——但日志需要一个到场的人去读，
而「单没出去」恰恰发生在人不在场的时候：L2 实时从 70c538c9 起整整一个重构周期
没下过一单，系统里的全部痕迹就是日志里那行 "too many values to unpack"。

判据是**纯函数**（读结果字典 / 状态字典，无 IO，返回 ``Alert`` 列表），所以判据
用例全是同步的；投递走 ``shared.alert_delivery``（去重、送达后才记键），替身与
``test_alert_delivery.py`` 同形。

时段闸门（``in_trading_hours``）是**注入**的：判据读字典里的那个字段，测试想测
哪个时段就写哪个值——不依赖墙上钟，收盘后跑也是同一套结果。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from backend.services.live_trading.services import tdx_exec_alerts as X

DAY = date(2026, 9, 24)


class FakeRedis:
    """原生形状的去重设施替身（``set(k, v, ex=)``，可注入读/写失败）。"""

    def __init__(self, fail_set: bool = False, fail_get: bool = False) -> None:
        self.store: dict[str, object] = {}
        self.fail_set = fail_set
        self.fail_get = fail_get

    def set(self, key, value, ex=None):
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


def _kinds(alerts) -> list[str]:
    return [a.kind for a in alerts]


# ── 判据：滚动买卖腿 ──────────────────────────────────────────────────
def _rolling(**over) -> dict:
    """一次 ``run_rolling_push`` 的结果（只铺判据读得到的字段）。"""
    base: dict = {
        "success": True,
        "error": None,
        "positions_error": "",
        "signals_suppressed": 0,
        "in_trading_hours": True,
        "failed_orders": [],
    }
    base.update(over)
    return base


def test_a_clean_rolling_run_has_nothing_to_say() -> None:
    assert X.rolling_alerts(_rolling()) == []


def test_rolling_abort_reports_that_nothing_was_submitted() -> None:
    """早退型失败（无分数 / 桥没配）：一条就够，且必须说清「没有任何委托」。"""
    alerts = X.rolling_alerts(
        _rolling(success=False, error="TDX_BRIDGE_URL/TOKEN 未配置")
    )
    assert _kinds(alerts) == [X.KIND_ABORTED]
    assert "未配置" in alerts[0].title
    assert "没有产生任何委托" in alerts[0].content
    assert alerts[0].level == "error"


def test_a_successful_rolling_run_with_an_error_note_is_not_an_abort() -> None:
    """``error`` 字段在下单失败时也带值——只有 ``success=False`` 才算整条腿没跑起来。"""
    alerts = X.rolling_alerts(
        _rolling(error="x", failed_orders=[{"symbol": "SH600000", "side": "sell"}])
    )
    assert _kinds(alerts) == [X.KIND_ORDERS_FAILED]


def test_unreadable_positions_are_reported_with_how_many_signals_were_dropped() -> None:
    alerts = X.rolling_alerts(
        _rolling(
            positions_error="通达信桥持仓拉取失败",
            signals_suppressed=3,
            in_trading_hours=True,
        )
    )
    assert _kinds(alerts) == [X.KIND_POSITIONS_UNREADABLE]
    assert "3 条" in alerts[0].title
    assert "通达信桥持仓拉取失败" in alerts[0].content
    assert "整体未发" in alerts[0].content


def test_unreadable_positions_out_of_hours_are_not_pushed() -> None:
    """非交易时段通达信客户端通常不在线：此刻「持仓读不到」没让任何可做的事落空。"""
    assert (
        X.rolling_alerts(
            _rolling(
                positions_error="桥未连接", signals_suppressed=3, in_trading_hours=False
            )
        )
        == []
    )


def test_unreadable_positions_with_nothing_to_suppress_are_not_pushed() -> None:
    """没有信号可压时这条失败没有后果（一条买卖判断都没有的时段天天报警=假警）。"""
    assert X.rolling_alerts(_rolling(positions_error="桥未连接")) == []


def test_failed_legs_are_listed_with_cap_and_remainder_count() -> None:
    failed = [
        {
            "symbol": f"SH60000{i}",
            "side": "buy" if i % 2 else "sell",
            "error": f"拒因{i}",
        }
        for i in range(7)
    ]
    alerts = X.rolling_alerts(_rolling(failed_orders=failed))
    assert _kinds(alerts) == [X.KIND_ORDERS_FAILED]
    assert "7 条" in alerts[0].title
    body = alerts[0].content
    assert "另 2 条" in body
    assert body.count("拒因") == X._MAX_LISTED
    assert "不会自动重发" in body


# ── 判据：L2 实时腿 ───────────────────────────────────────────────────
def _l2(**over) -> dict:
    base: dict = {
        "cycle_error": None,
        "positions_error": None,
        "trigger_skipped_symbols": 0,
        "exec_error": None,
        "orders_failed": [],
        "orders_failed_count": 0,
        "pool_starved_cycles": 0,
        "capture_stale": False,
        "in_trading_hours": True,
    }
    base.update(over)
    return base


def test_a_clean_l2_cycle_has_nothing_to_say() -> None:
    assert X.l2_alerts(_l2()) == []


def test_l2_cycle_error_is_reported_as_this_cycle_only() -> None:
    """整周期异常：说清「本周期整段跳过、循环还在跑」，免得被当成腿已经死了。"""
    alerts = X.l2_alerts(_l2(cycle_error="too many values to unpack"))
    assert _kinds(alerts) == [X.KIND_CYCLE_ERROR]
    assert "too many values to unpack" in alerts[0].content
    assert "下一周期自动重试" in alerts[0].content


def test_l2_unreadable_positions_report_the_symbols_it_could_not_judge() -> None:
    alerts = X.l2_alerts(
        _l2(positions_error="持仓查询失败: 桥断开", trigger_skipped_symbols=6)
    )
    assert _kinds(alerts) == [X.KIND_POSITIONS_UNREADABLE]
    assert "6 只" in alerts[0].content
    assert "在途单重挂一并跳过" in alerts[0].content


def test_l2_unreadable_positions_out_of_hours_are_not_pushed() -> None:
    assert (
        X.l2_alerts(
            _l2(
                positions_error="桥断开",
                trigger_skipped_symbols=6,
                in_trading_hours=False,
            )
        )
        == []
    )


def test_l2_exec_error_is_reported() -> None:
    alerts = X.l2_alerts(_l2(exec_error="execute_mode=off 仅预警不执行"))
    assert _kinds(alerts) == [X.KIND_EXEC_ERROR]
    assert "没有送到券商" in alerts[0].content


def test_l2_failed_legs_use_the_count_not_the_truncated_list() -> None:
    """状态里的清单有上限（面板要一屏读完），条数必须另记——否则通知少报。"""
    failed = [
        {"symbol": f"SH60000{i}", "side": "buy", "error": "被拒"} for i in range(3)
    ]
    alerts = X.l2_alerts(_l2(orders_failed=failed[:1], orders_failed_count=3))
    assert _kinds(alerts) == [X.KIND_ORDERS_FAILED]
    assert "3 条" in alerts[0].title


def test_l2_stale_capture_is_reported_as_stalled_and_trigger_closed() -> None:
    alerts = X.l2_alerts(_l2(capture_stale=True))
    assert _kinds(alerts) == [X.KIND_STALLED]
    assert alerts[0].level == "warning"
    assert "停摆" in alerts[0].title
    assert "采集链路已陈旧" in alerts[0].content
    # 说清「循环还活着、只是不下单」——否则收到的人第一反应是去重启进程
    assert "触发执行已关闭" in alerts[0].content


def test_l2_pool_starvation_needs_consecutive_cycles_before_it_rings() -> None:
    """开盘头几分钟池子还在构建：单周期不足报警 = 每天一次的假警。"""
    threshold = X.POOL_STARVED_ALERT_CYCLES
    assert X.l2_alerts(_l2(pool_starved_cycles=threshold - 1)) == []
    alerts = X.l2_alerts(_l2(pool_starved_cycles=threshold))
    assert _kinds(alerts) == [X.KIND_STALLED]
    assert str(threshold) in alerts[0].content


def test_l2_stall_out_of_hours_is_not_pushed() -> None:
    assert X.l2_alerts(_l2(capture_stale=True, in_trading_hours=False)) == []


def test_several_kinds_in_one_cycle_are_all_reported() -> None:
    """一个周期可以同时坏几件事（各自去重，互不吃掉）。"""
    alerts = X.l2_alerts(
        _l2(
            cycle_error="boom",
            exec_error="执行段未跑",
            orders_failed_count=1,
            orders_failed=[{"symbol": "SH600000", "side": "buy", "error": "被拒"}],
        )
    )
    assert _kinds(alerts) == [
        X.KIND_CYCLE_ERROR,
        X.KIND_EXEC_ERROR,
        X.KIND_ORDERS_FAILED,
    ]


# ── 键形（去重口径的实体） ────────────────────────────────────────────
def test_alert_key_is_day_family_kind() -> None:
    key = X.alert_key(X.FAMILY_L2, X.KIND_STALLED, DAY)
    assert "2026-09-24" in key
    assert X.FAMILY_L2 in key and X.KIND_STALLED in key
    assert key != X.alert_key(X.FAMILY_ROLLING, X.KIND_STALLED, DAY)
    assert key != X.alert_key(X.FAMILY_L2, X.KIND_STALLED, DAY + timedelta(days=1))


def test_the_resolved_key_is_per_family_not_per_kind() -> None:
    """三类一起好时推三条「已恢复」是刷屏：恢复通知按腿，一天一条。"""
    assert X.resolved_key(X.FAMILY_L2, DAY) == X.resolved_key(X.FAMILY_L2, DAY)
    assert X.resolved_key(X.FAMILY_L2, DAY) != X.resolved_key(X.FAMILY_ROLLING, DAY)
    assert X.KIND_STALLED not in X.resolved_key(X.FAMILY_L2, DAY)


def test_aborted_is_not_resolvable() -> None:
    """``aborted`` 每次运行重新判定：今天的推理没信号，不会因为下一轮干净就「恢复」。"""
    assert X.KIND_ABORTED not in X.RESOLVABLE_KINDS


# ── 投递 ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_same_kind_is_pushed_once_a_day_per_family() -> None:
    redis, notifier = FakeRedis(), SpyNotifier()
    alerts = [X.Alert(kind=X.KIND_STALLED, level="warning", title="t", content="c")]
    first = await X.alert_exec_leg(
        X.FAMILY_L2,
        alerts,
        user_id="u1",
        redis=redis,
        notifier_factory=lambda: notifier,
        day=DAY,
    )
    second = await X.alert_exec_leg(
        X.FAMILY_L2,
        alerts,
        user_id="u1",
        redis=redis,
        notifier_factory=lambda: notifier,
        day=DAY,
    )
    assert (first, second) == (1, 0)
    assert len(notifier.calls) == 1


@pytest.mark.asyncio
async def test_the_two_families_do_not_share_a_dedupe_key() -> None:
    """同一天两条腿都停摆 ⇒ 推两条（各自的分母、各自的处置）。"""
    redis, notifier = FakeRedis(), SpyNotifier()
    alerts = [X.Alert(kind=X.KIND_STALLED, level="warning", title="t", content="c")]
    for family in (X.FAMILY_L2, X.FAMILY_ROLLING):
        await X.alert_exec_leg(
            family,
            alerts,
            user_id="u1",
            redis=redis,
            notifier_factory=lambda: notifier,
            day=DAY,
        )
    assert len(notifier.calls) == 2


@pytest.mark.asyncio
async def test_a_new_day_is_pushed_again() -> None:
    """「一天一次」按交易日算：昨天推过不挡今天。"""
    redis, notifier = FakeRedis(), SpyNotifier()
    alerts = [X.Alert(kind=X.KIND_STALLED, level="warning", title="t", content="c")]
    for day in (DAY, DAY + timedelta(days=1)):
        await X.alert_exec_leg(
            X.FAMILY_L2,
            alerts,
            user_id="u1",
            redis=redis,
            notifier_factory=lambda: notifier,
            day=day,
        )
    assert len(notifier.calls) == 2


@pytest.mark.asyncio
async def test_each_kind_is_pushed_on_its_own_key() -> None:
    redis, notifier = FakeRedis(), SpyNotifier()
    alerts = [
        X.Alert(kind=X.KIND_CYCLE_ERROR, level="error", title="a", content="a"),
        X.Alert(kind=X.KIND_EXEC_ERROR, level="error", title="b", content="b"),
    ]
    sent = await X.alert_exec_leg(
        X.FAMILY_L2,
        alerts,
        user_id="u1",
        redis=redis,
        notifier_factory=lambda: notifier,
        day=DAY,
    )
    assert sent == 2


@pytest.mark.asyncio
async def test_an_undelivered_alert_is_retried_next_cycle() -> None:
    """没送达就不记键：下一个周期（60s 后）还会再试——故障还在。"""
    redis = FakeRedis()
    bad = SpyNotifier(ok=False)
    alerts = [X.Alert(kind=X.KIND_STALLED, level="warning", title="t", content="c")]
    assert not await X.alert_exec_leg(
        X.FAMILY_L2,
        alerts,
        user_id="u1",
        redis=redis,
        notifier_factory=lambda: bad,
        day=DAY,
    )
    assert redis.store == {}
    good = SpyNotifier()
    assert await X.alert_exec_leg(
        X.FAMILY_L2,
        alerts,
        user_id="u1",
        redis=redis,
        notifier_factory=lambda: good,
        day=DAY,
    )
    assert len(good.calls) == 1


@pytest.mark.asyncio
async def test_a_notifier_exception_never_escapes_the_exec_leg() -> None:
    """刚跑完的一次运行里可能已经有真单出去了：通知层不许把它变成异常。"""
    sent = await X.alert_exec_leg(
        X.FAMILY_ROLLING,
        [X.Alert(kind=X.KIND_ORDERS_FAILED, level="error", title="t", content="c")],
        user_id="u1",
        redis=FakeRedis(),
        notifier_factory=lambda: SpyNotifier(raises=True),
        day=DAY,
    )
    assert sent == 0


@pytest.mark.asyncio
async def test_no_alerts_means_no_push_and_no_notifier() -> None:
    def boom():
        raise AssertionError("没有要推的事不该现造通知器")

    assert (
        await X.alert_exec_leg(
            X.FAMILY_L2,
            [],
            user_id="u1",
            redis=FakeRedis(),
            notifier_factory=boom,
            day=DAY,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_a_missing_account_coordinate_is_not_sent_to_nobody() -> None:
    notifier = SpyNotifier()
    sent = await X.alert_exec_leg(
        X.FAMILY_L2,
        [X.Alert(kind=X.KIND_STALLED, level="warning", title="t", content="c")],
        user_id="",
        redis=FakeRedis(),
        notifier_factory=lambda: notifier,
        day=DAY,
    )
    assert sent == 0
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_seen_keeps_a_broken_redis_from_spamming_every_cycle() -> None:
    """去重设施读不出来时，循环型调用方靠进程内 ``seen`` 兜住（60s 一条=噪音）。"""
    notifier = SpyNotifier()
    seen = set()
    alerts = [X.Alert(kind=X.KIND_STALLED, level="warning", title="t", content="c")]
    for _ in range(5):
        await X.alert_exec_leg(
            X.FAMILY_L2,
            alerts,
            user_id="u1",
            redis=FakeRedis(fail_get=True),
            notifier_factory=lambda: notifier,
            day=DAY,
            seen=seen,
        )
    assert len(notifier.calls) == 1


# ── 恢复通知 ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_resolve_needs_a_dedupe_facility() -> None:
    assert (
        await X.resolve_exec_leg(X.FAMILY_L2, user_id="u1", redis=None, day=DAY)
        is False
    )


@pytest.mark.asyncio
async def test_resolve_says_nothing_when_today_never_reported() -> None:
    """没报过就无所谓「恢复」——否则每天第一轮干净运行都推一条。"""
    notifier = SpyNotifier()
    assert (
        await X.resolve_exec_leg(
            X.FAMILY_L2,
            user_id="u1",
            redis=FakeRedis(),
            notifier_factory=lambda: notifier,
            day=DAY,
        )
        is False
    )
    assert notifier.calls == []


@pytest.mark.asyncio
async def test_resolve_fires_once_after_a_reported_failure_and_a_clean_run() -> None:
    redis, notifier = FakeRedis(), SpyNotifier()
    alerts = [
        X.Alert(kind=X.KIND_POSITIONS_UNREADABLE, level="error", title="t", content="c")
    ]
    await X.alert_exec_leg(
        X.FAMILY_L2,
        alerts,
        user_id="u1",
        redis=redis,
        notifier_factory=lambda: notifier,
        day=DAY,
    )

    assert (
        await X.resolve_exec_leg(
            X.FAMILY_L2,
            user_id="u1",
            redis=redis,
            notifier_factory=lambda: notifier,
            day=DAY,
            at="10:32",
        )
        is True
    )
    assert len(notifier.calls) == 2
    _uid, title, content, level = notifier.calls[1]
    assert "已恢复" in title
    assert X.KIND_POSITIONS_UNREADABLE in content  # 说清今天报过哪几类
    assert "10:32" in content
    assert level == "success"

    # 同一天再干净也不再推（恢复通知一天一条，按腿）
    assert (
        await X.resolve_exec_leg(
            X.FAMILY_L2,
            user_id="u1",
            redis=redis,
            notifier_factory=lambda: notifier,
            day=DAY,
        )
        is False
    )
    assert len(notifier.calls) == 2


@pytest.mark.asyncio
async def test_resolve_is_per_family() -> None:
    """L2 报过、rolling 没报过 ⇒ 只有 L2 的恢复通知（另一条腿没事可恢复）。"""
    redis, notifier = FakeRedis(), SpyNotifier()
    await X.alert_exec_leg(
        X.FAMILY_L2,
        [X.Alert(kind=X.KIND_STALLED, level="warning", title="t", content="c")],
        user_id="u1",
        redis=redis,
        notifier_factory=lambda: notifier,
        day=DAY,
    )
    assert (
        await X.resolve_exec_leg(
            X.FAMILY_ROLLING,
            user_id="u1",
            redis=redis,
            notifier_factory=lambda: notifier,
            day=DAY,
        )
        is False
    )


@pytest.mark.asyncio
async def test_resolve_is_not_sent_when_today_only_aborted() -> None:
    """``aborted`` 不在可恢复之列：它每次运行重新判定。"""
    redis, notifier = FakeRedis(), SpyNotifier()
    await X.alert_exec_leg(
        X.FAMILY_ROLLING,
        [X.Alert(kind=X.KIND_ABORTED, level="error", title="t", content="c")],
        user_id="u1",
        redis=redis,
        notifier_factory=lambda: notifier,
        day=DAY,
    )
    assert (
        await X.resolve_exec_leg(
            X.FAMILY_ROLLING,
            user_id="u1",
            redis=redis,
            notifier_factory=lambda: notifier,
            day=DAY,
        )
        is False
    )
    assert len(notifier.calls) == 1  # 只有那条 aborted


# ── 夹具形状 = 生产形状 ────────────────────────────────────────────────
def test_every_field_the_judges_read_is_written_by_the_l2_loop() -> None:
    """判据读的字段必须真的由生产者写。

    判据是「读字典 → 判断」，字段名漂移两边**都不报错**：状态里没有该字段 ⇒
    ``.get`` 得 None ⇒ 判据恒不触发 ⇒ 告警面静默消失（本文件全绿）。这条守卫把
    生产者与消费者的字段名钉在一起。
    """
    from backend.services.live_trading.services.tdx_l2_realtime import realtime_status

    for field in (
        "cycle_error",
        "positions_error",
        "trigger_skipped_symbols",
        "session_skipped_symbols",
        "exec_error",
        "orders_failed",
        "orders_failed_count",
        "pool_starved_cycles",
        "capture_stale",
        "in_trading_hours",
    ):
        assert field in realtime_status, f"L2 状态里没有告警判据要读的字段：{field}"
