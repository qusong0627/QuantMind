"""市场定时同步派发的回归测试。

锁的是**标记与派发的先后顺序**：last_run 标记同时承担「去重」职责，
先写标记再派发，等于把「打算跑」当成「已经跑」，一次 broker 抖动就能
让当天同步永久失效（2026-09-18 Redis stop-writes 事故即此路径）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from backend.services.engine.tasks import market_sync_scheduler as sched


class _FakeCelery:
    """只记下发过什么，不碰 broker。"""

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.sent: list[str] = []
        self.fail_for = fail_for or set()

    def send_task(self, name: str, args: list[Any], queue: str) -> None:
        market = args[0]
        if market in self.fail_for:
            raise ConnectionError("broker down")
        self.sent.append(market)


class _RedisStub:
    """最小 Redis 桩：调度器只用 get/set/exists（重试记账走这里）。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.data.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value

    def exists(self, key: str) -> bool:
        return key in self.data


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把调度器的配置与标记替换成可控对象。

    配置里的 `time` 取**当前真实分钟**，这样 `dispatch_due_syncs` 内部的
    `datetime.now()` 无需冻结也必然「到点」——比 mock 掉 datetime 少一层耦合。
    """
    marks: set[str] = set()
    catchups: set[str] = set()
    stub = _RedisStub()
    state: dict[str, Any] = {"marks": marks, "catchups": catchups, "redis": stub}

    monkeypatch.setattr(sched, "_redis", lambda: stub)

    def _fake_get_schedule(_market: str) -> dict[str, Any]:
        return {
            "enabled": True,
            "time": datetime.now().strftime("%H:%M"),
            "days": 5,
            "datasets": [],
            "with_qlib": False,
        }

    monkeypatch.setattr(sched, "get_schedule", _fake_get_schedule)
    monkeypatch.setattr(sched, "_has_run", lambda market, date_str: (market, date_str) in marks)
    monkeypatch.setattr(sched, "_mark_run", lambda market, date_str: marks.add((market, date_str)))
    monkeypatch.setattr(
        sched, "_catchup_done_today", lambda market, date_str: (market, date_str) in catchups
    )
    monkeypatch.setattr(
        sched, "_mark_catchup", lambda market, date_str: catchups.add((market, date_str))
    )
    # 日历换替身：默认「答不了」= 不补跑，既有用例与真实日历解耦。
    # 跨日补跑用例自己覆盖这个替身。
    monkeypatch.setattr(sched, "previous_trading_day", lambda market, today: None)
    # 交易日历门换替身：既有测试与真实日历解耦（到点/标记纪律与日历无关）
    monkeypatch.setattr(sched, "no_data_window", lambda market, now: None)
    return state


def _install_celery(monkeypatch: pytest.MonkeyPatch, fake: _FakeCelery) -> None:
    import backend.services.engine.qlib_app.celery_config as cfg

    monkeypatch.setattr(cfg, "celery_app", fake, raising=True)


def test_marks_run_after_successful_dispatch(monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]) -> None:
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"US": "QuantUS 美股"})

    result = sched.dispatch_due_syncs()

    assert fake.sent == ["US"]
    assert result["dispatched"] == ["US"]
    assert {market for market, _date in env["marks"]} == {"US"}, "派发成功后必须写标记"


def test_dispatch_failure_leaves_no_marker_so_next_minute_retries(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """核心回归：派发抛错时，绝不能留下「今天已跑」的假记录。"""
    fake = _FakeCelery(fail_for={"US"})
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"US": "QuantUS 美股"})

    result = sched.dispatch_due_syncs()

    assert result["dispatched"] == []
    assert env["marks"] == set(), "派发失败不得写 last_run 标记，否则当天永不重试"


def test_marker_prevents_second_dispatch_same_day(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"US": "QuantUS 美股"})

    sched.dispatch_due_syncs()
    second = sched.dispatch_due_syncs()

    assert second["dispatched"] == []
    assert fake.sent == ["US"], "同日不得重复派发"


def test_one_market_failure_does_not_block_others(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    fake = _FakeCelery(fail_for={"HK"})
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"HK": "QuantHK 港股", "US": "QuantUS 美股"})

    result = sched.dispatch_due_syncs()

    assert result["dispatched"] == ["US"]
    assert {market for market, _date in env["marks"]} == {"US"}, "失败的市场不得被标记"


# ---------------------------------------------------------------------------
# 到点判据（审计 H4）：迟到仍派发、未到不抢跑、日键仍锁当日一次
# ---------------------------------------------------------------------------


class _FrozenDatetime(datetime):
    """固定时钟替身：dispatch 内部的 ``datetime.now()`` 走这里。"""

    fixed = datetime(2026, 10, 10, 4, 35, 20)

    @classmethod
    def now(cls, tz=None):
        return cls.fixed


def _freeze_uniform_schedule(
    monkeypatch: pytest.MonkeyPatch, cfg_time: str, at: datetime
) -> None:
    class _At(_FrozenDatetime):
        fixed = at

    monkeypatch.setattr(sched, "datetime", _At)
    monkeypatch.setattr(
        sched,
        "get_schedule",
        lambda _m: {
            "enabled": True,
            "time": cfg_time,
            "days": 5,
            "datasets": [],
            "with_qlib": False,
        },
    )


def test_tick_late_by_minutes_still_dispatches_exactly_once(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """H4 核心回归：worker 忙过 60s，beat 迟到 5 分钟仍要派发。

    旧「精确分钟相等」判据下这一天整班静默跳发；修后迟到即补跑，
    且当日日键仍锁住「至多一次」。
    """
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"US": "QuantUS 美股"})
    _freeze_uniform_schedule(monkeypatch, "04:30", datetime(2026, 10, 10, 4, 35, 20))

    first = sched.dispatch_due_syncs()
    second = sched.dispatch_due_syncs()

    assert first["dispatched"] == ["US"], "迟到 5 分钟必须仍派发"
    assert second["dispatched"] == [], "补跑仍受当日日键约束，不得重复"
    assert fake.sent == ["US"]


def test_missed_by_hours_catches_up_after_restart(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """重启补齐：过点数小时且当日无标记，仍是当日首次到点。"""
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"US": "QuantUS 美股"})
    _freeze_uniform_schedule(monkeypatch, "04:30", datetime(2026, 10, 10, 9, 10, 0))

    assert sched.dispatch_due_syncs()["dispatched"] == ["US"]


def test_before_the_configured_time_still_waits(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """反向守卫：`>=` 不是「随时都跑」——未到点不得抢跑。"""
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"US": "QuantUS 美股"})
    _freeze_uniform_schedule(monkeypatch, "04:30", datetime(2026, 10, 10, 4, 25, 0))

    assert sched.dispatch_due_syncs()["dispatched"] == []
    assert fake.sent == []


# ---------------------------------------------------------------------------
# 交易日历门（P2-3 / 审计 M7）：连续非交易日跳发、留原因、不落标记
# ---------------------------------------------------------------------------


def test_idle_calendar_window_skips_with_reason_and_no_marker(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """{今日, 昨日} 都非交易日 → 不派发、不写标记、skipped 带原因（值班面可见）。"""
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"US": "QuantUS 美股"})
    reason = "连续非交易日（2026-10-10、2026-10-11 均非交易日），无新数据可同步"
    monkeypatch.setattr(sched, "no_data_window", lambda market, now: reason)

    result = sched.dispatch_due_syncs()

    assert result["dispatched"] == []
    assert result["skipped"] == {"US": reason}
    assert fake.sent == []
    assert env["marks"] == set(), "跳发不是「跑过了」，不得写 last_run 标记"


def test_calendar_skip_leaves_the_day_open_for_a_later_tick(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """跳发不烧当日名额：后续 tick 门放行时仍是当日首次到点，照常派发。"""
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"US": "QuantUS 美股"})
    answer: dict[str, str | None] = {"reason": "连续非交易日"}
    monkeypatch.setattr(sched, "no_data_window", lambda market, now: answer["reason"])

    first = sched.dispatch_due_syncs()
    assert first["dispatched"] == [] and first["skipped"] == {"US": "连续非交易日"}

    answer["reason"] = None
    second = sched.dispatch_due_syncs()

    assert second["dispatched"] == ["US"]
    assert second["skipped"] == {}
    assert fake.sent == ["US"]


# ---------------------------------------------------------------------------
# 跨日补跑（2026-11-05 崩机）：当日班次还没到点，但上一交易日没跑成
# ---------------------------------------------------------------------------


def _freeze_missing_previous_day(
    monkeypatch: pytest.MonkeyPatch, cfg_time: str, at: datetime
) -> None:
    _freeze_uniform_schedule(monkeypatch, cfg_time, at)
    # 上一交易日钉死：补跑判据只关心「那天有没有标记」，不关心真实日历
    monkeypatch.setattr(
        sched, "previous_trading_day", lambda market, today: date(2026, 11, 5)
    )


def test_missed_previous_day_is_caught_up_before_todays_slot(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """CN 20:55 的班次在崩机夜丢失 → 次日 09:10 开机必须立刻补跑。

    事故形态：宿主 21:44 崩机、次日 09:03 才起。旧逻辑只问「今天 20:55 到点没」，
    没到就跳过 ⇒ 落后数据挂一整天，批量推理两班全撞数据门禁。
    """
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"A": "QuantDB A股"})
    _freeze_missing_previous_day(monkeypatch, "20:55", datetime(2026, 11, 6, 9, 10, 0))

    result = sched.dispatch_due_syncs()

    assert fake.sent == ["A"]
    assert result["catchup"] == ["A"]
    assert result["dispatched"] == [], "补跑不是当天班次，不能混进 dispatched"
    assert env["marks"] == set(), "补跑不得占用当天的 last_run 名额"
    assert {m for m, _d in env["catchups"]} == {"A"}


def test_catchup_is_idempotent_within_the_day(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"A": "QuantDB A股"})
    _freeze_missing_previous_day(monkeypatch, "20:55", datetime(2026, 11, 6, 9, 10, 0))

    sched.dispatch_due_syncs()
    second = sched.dispatch_due_syncs()

    assert second["catchup"] == []
    assert fake.sent == ["A"], "同一天不得重复补跑"


def test_no_catchup_when_previous_day_already_ran(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """上一交易日跑过了 → 未到点就安静等待（回归守卫：不许变成「随时都跑」）。"""
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"A": "QuantDB A股"})
    _freeze_missing_previous_day(monkeypatch, "20:55", datetime(2026, 11, 6, 9, 10, 0))
    env["marks"].add(("A", "2026-11-05"))

    result = sched.dispatch_due_syncs()

    assert result == {"now": "09:10", "dispatched": [], "catchup": [], "skipped": {}}
    assert fake.sent == []


def test_catchup_does_not_consume_todays_scheduled_slot(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """补跑之后，当晚到点仍必须正常跑一次——否则当天的新数据永远不同步。"""
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"A": "QuantDB A股"})
    _freeze_missing_previous_day(monkeypatch, "20:55", datetime(2026, 11, 6, 9, 10, 0))
    assert sched.dispatch_due_syncs()["catchup"] == ["A"]

    _freeze_missing_previous_day(monkeypatch, "20:55", datetime(2026, 11, 6, 20, 55, 5))
    evening = sched.dispatch_due_syncs()

    assert evening["dispatched"] == ["A"]
    assert evening["catchup"] == []
    assert fake.sent == ["A", "A"]


def test_calendar_unknown_does_not_trigger_catchup(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """日历答不了时不外推：补跑的代价是一整轮同步，宁可这轮不补。"""
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"A": "QuantDB A股"})
    _freeze_uniform_schedule(monkeypatch, "20:55", datetime(2026, 11, 6, 9, 10, 0))
    monkeypatch.setattr(sched, "previous_trading_day", lambda market, today: None)

    assert sched.dispatch_due_syncs()["catchup"] == []
    assert fake.sent == []


# ---------------------------------------------------------------------------
# 2026-11-06：补跑判据从「派发标记」改成「数据是否真的到位」
# ---------------------------------------------------------------------------


def test_catchup_fires_when_prev_day_data_missing_despite_marker(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """崩机场景回归：上一交易日的派发标记写了，但任务被杀、数据没落齐 → 仍须补跑。

    2026-11-05 20:55 派发、21:44 宿主崩机；次日开机后判据看标记 → 认定「跑过了」，
    `daily_unadjusted` 整天停在崩机前一天，当天候选池空。
    """
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"A": "QuantDB A股"})
    _freeze_missing_previous_day(monkeypatch, "20:55", datetime(2026, 11, 6, 9, 10, 0))
    env["marks"].add(("A", "2026-11-05"))
    monkeypatch.setattr(sched, "_market_data_covers", lambda m, d: False)

    result = sched.dispatch_due_syncs()

    assert fake.sent == ["A"], "数据没到位就必须补跑，标记不构成免补理由"
    assert result["catchup"] == ["A"]


def test_no_catchup_when_data_already_covers_prev_day(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """数据已到位：即使当天没有任何标记也不补（不许变成「随时都跑」）。"""
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"A": "QuantDB A股"})
    _freeze_missing_previous_day(monkeypatch, "20:55", datetime(2026, 11, 6, 9, 10, 0))
    monkeypatch.setattr(sched, "_market_data_covers", lambda m, d: True)

    result = sched.dispatch_due_syncs()

    assert result["catchup"] == []
    assert fake.sent == []


def test_retry_allowed_is_rate_limited_and_capped(env: dict[str, Any]) -> None:
    """数据缺时的重试必须限频限次：不能每分钟重派，也不能无限补。"""
    now = datetime(2026, 11, 6, 9, 10, 0)
    date_str = "2026-11-06"

    assert sched._retry_allowed("A", now, date_str) is True  # 从未派发

    sched._note_attempt("A", now, date_str)
    assert sched._retry_allowed("A", now, date_str) is False, "45 分钟内不许连补"

    later = datetime(2026, 11, 6, 10, 0, 0)
    assert sched._retry_allowed("A", later, date_str) is True

    sched._note_attempt("A", later, date_str)
    sched._note_attempt("A", datetime(2026, 11, 6, 11, 0, 0), date_str)
    assert (
        sched._retry_allowed("A", datetime(2026, 11, 6, 12, 0, 0), date_str) is False
    ), "满 3 次后当天不再重派"


def test_due_slot_retries_when_data_still_missing(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]
) -> None:
    """计划班次跑空（上游迟到）：数据仍缺时，到点后的下一档再派一次。"""
    fake = _FakeCelery()
    _install_celery(monkeypatch, fake)
    monkeypatch.setattr(sched, "MARKETS", {"A": "QuantDB A股"})
    _freeze_uniform_schedule(monkeypatch, "20:55", datetime(2026, 11, 6, 21, 45, 0))
    monkeypatch.setattr(
        sched, "previous_trading_day", lambda market, today: date(2026, 11, 5)
    )
    monkeypatch.setattr(sched, "_market_data_covers", lambda m, d: False)
    env["marks"].add(("A", "2026-11-06"))  # 20:55 班次已派发但没拿到数据
    sched._note_attempt("A", datetime(2026, 11, 6, 20, 55, 5), "2026-11-06")

    result = sched.dispatch_due_syncs()

    assert result["dispatched"] == ["A"]
    assert fake.sent == ["A"]


def test_market_data_covers_requires_all_key_partitions(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 股要看 daily_forward + daily_unadjusted 两个分区：只到一个不算齐。"""
    root = tmp_path / "quantdb"
    for rel in ("1_kline_data/daily_forward", "1_kline_data/daily_unadjusted"):
        (root / rel / "dt=20261104").mkdir(parents=True)
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(root))

    assert sched._market_data_covers("A", date(2026, 11, 4)) is True
    assert sched._market_data_covers("A", date(2026, 11, 5)) is False

    # 只补 daily_unadjusted 仍不算齐（min 取两者）——必须两个关键分区都到 11-05
    (root / "1_kline_data/daily_unadjusted" / "dt=20261105").mkdir()
    assert sched._market_data_covers("A", date(2026, 11, 5)) is False

    (root / "1_kline_data/daily_forward" / "dt=20261105").mkdir()
    assert sched._market_data_covers("A", date(2026, 11, 5)) is True


def test_market_data_covers_unknown_when_root_unreadable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录读不到 = 探针不可用（None），调用方要退回标记判据，别当成「缺数据」。"""
    monkeypatch.setenv("QM_QUANTDB_DATA_DIR", str(tmp_path / "missing"))

    assert sched._market_data_covers("A", date(2026, 11, 5)) is None
