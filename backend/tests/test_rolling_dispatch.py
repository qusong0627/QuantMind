"""滚动派发核心测试（P1）：就绪裁决纯函数 + 守卫 + 全流程（IO 层打桩）。

真库/真日历的部分留给端点级验收（验收 ①/②/③）；本文件锁死：
- 就绪裁决五类跳过的判定与优先级（数据滞后守卫是上游断链的唯一闸门）；
- 内存守卫解析与取向；
- busy 探针「探针失败按 busy 处理」的保守取向；
- 派发主流程的幂等语义：duplicate 不提交、busy 409 不消耗 attempts、
  dry_run 零副作用、提交载荷六键 split + rolling_meta 完备。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.services.engine.training import rolling_dispatch as rd
from backend.shared.training.recipe_registry import load_recipe, validate_recipe

FIXTURE = Path(__file__).parent / "fixtures" / "rollingWindowGolden.json"


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def small_days(golden) -> list[date]:
    return [date.fromisoformat(d) for d in golden["small_calendar"]["trading_days"]]


def _synthetic_recipe():
    """5/3/2 小窗口配方：与 golden small_calendar 对齐（30 个交易日）。"""
    return validate_recipe(
        {
            "recipe_id": "unittest_recipe",
            "market": "CN",
            "factor_market": "CUSTOM",
            "factor_source": "l1_factors",
            "target_horizon_days": 1,
            "window_policy": {
                "train_days": 5,
                "valid_days": 3,
                "test_days": 2,
                "mode": "sliding",
            },
            "payload": {
                "model_type": "lightgbm",
                "factor_source": "l1_factors",
                "features": ["f1"],
            },
        }
    )


# ---------------------------------------------------------------------------
# compute_plan：就绪 / 跳过矩阵
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compute_plan_ready_exact_window(small_days):
    # today = 最后一个日历日的下周一；factor 覆盖到 04-10（周五）→ 无滞后
    result = rd.compute_plan(
        _synthetic_recipe(),
        calendar_days=small_days,
        factor_dates=small_days,
        today=date(2026, 4, 13),
    )
    assert result.ready is True and result.reason == "ok"
    window = result.window
    # anchor = 倒数第 horizon+lag+1 = 3 个交易日 → 04-08（idx 27）；purge=2。
    # 与 golden small_calendar（anchor 04-10 → train 03-24~03-30 / valid 04-02~04-06 /
    # test 04-09~04-10）整体前移 2 个交易日，逐段对得上。
    assert window.anchor_date == date(2026, 4, 8)
    assert (window.train_start, window.train_end) == (date(2026, 3, 20), date(2026, 3, 26))
    assert (window.valid_start, window.valid_end) == (date(2026, 3, 31), date(2026, 4, 2))
    assert (window.test_start, window.test_end) == (date(2026, 4, 7), date(2026, 4, 8))
    assert result.factor_max_date == "2026-04-10"


@pytest.mark.unit
def test_compute_plan_data_lag_blocks_stale_factor_data(small_days):
    # 因子分区缺最后三天（04-08 之后）→ max 04-07 < 今天前最后交易日 04-10
    result = rd.compute_plan(
        _synthetic_recipe(),
        calendar_days=small_days,
        factor_dates=small_days[:-3],
        today=date(2026, 4, 13),
    )
    assert result.ready is False
    assert result.reason == "data_lag"
    assert result.detail["last_completed_trading_day"] == "2026-04-10"
    assert result.detail["factor_max_date"] == "2026-04-07"


@pytest.mark.unit
def test_compute_plan_completed_session_excludes_today(small_days):
    # 今天是交易日（04-10）且数据已含当日 → 最后完成交易日 = 04-09，不因盘中缺当日而误判
    result = rd.compute_plan(
        _synthetic_recipe(),
        calendar_days=small_days,
        factor_dates=small_days,
        today=date(2026, 4, 10),
    )
    assert result.ready is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("calendar_days", "factor_dates", "reason"),
    [
        (None, ["2026-04-10"], "calendar_unavailable"),
        ([], ["2026-04-10"], "calendar_unavailable"),
        (["2026-04-10"], None, "factor_source_empty"),
        (["2026-04-10"], [], "factor_source_empty"),
    ],
)
def test_compute_plan_empty_inputs(calendar_days, factor_dates, reason):
    result = rd.compute_plan(
        _synthetic_recipe(),
        calendar_days=calendar_days,
        factor_dates=factor_dates,
        today=date(2026, 4, 13),
    )
    assert result.ready is False and result.reason == reason


@pytest.mark.unit
def test_compute_plan_insufficient_factor_span(small_days):
    # 分区数 ≤ horizon+lag（3）→ 一个完整标签都放不下（且新到不触发滞后守卫）
    result = rd.compute_plan(
        _synthetic_recipe(),
        calendar_days=small_days,
        factor_dates=small_days[-2:],
        today=date(2026, 4, 13),
    )
    assert result.ready is False and result.reason == "factor_dates_insufficient"


@pytest.mark.unit
def test_compute_plan_window_unavailable_on_short_calendar(small_days):
    result = rd.compute_plan(
        _synthetic_recipe(),
        calendar_days=small_days[:8],  # span=5+2+3+2+2=14 > 8
        factor_dates=small_days[:8],
        today=date(2026, 4, 13),
    )
    assert result.ready is False and result.reason == "window_unavailable"
    assert "交易日不足" in result.detail["error"]


@pytest.mark.unit
def test_compute_plan_anchor_override_replays_old_window(small_days):
    result = rd.compute_plan(
        _synthetic_recipe(),
        calendar_days=small_days,
        factor_dates=small_days,
        today=date(2026, 4, 13),
        anchor_override="2026-04-02",  # 早于默认 anchor，手动回放
    )
    assert result.ready is True
    assert result.window.anchor_date == date(2026, 4, 2)


@pytest.mark.unit
def test_compute_plan_anchor_override_rejects_future_and_uncovered(small_days):
    future = rd.compute_plan(
        _synthetic_recipe(),
        calendar_days=small_days,
        factor_dates=small_days,
        today=date(2026, 4, 13),
        anchor_override="2026-05-01",
    )
    assert future.ready is False and future.reason == "anchor_after_calendar"
    uncovered = rd.compute_plan(
        _synthetic_recipe(),
        calendar_days=small_days,
        factor_dates=small_days[:20],  # 覆盖到 03-27
        today=date(2026, 4, 13),
        anchor_override="2026-04-08",
    )
    assert uncovered.ready is False and uncovered.reason == "anchor_beyond_factor_coverage"


@pytest.mark.unit
def test_compute_plan_factor_history_shorter_than_train_start(small_days):
    # 日历完整（窗口算得出），但因子分区从 03-23 才开始 → 盖不住 train_start 03-20
    factor = [d for d in small_days if d >= date(2026, 3, 23)]
    result = rd.compute_plan(
        _synthetic_recipe(),
        calendar_days=small_days,
        factor_dates=factor,
        today=date(2026, 4, 13),
    )
    assert result.ready is False and result.reason == "factor_history_insufficient"
    assert result.detail["train_start"] == "2026-03-20"


# ---------------------------------------------------------------------------
# planned 陈旧裁决
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, False),
        ({"status": "dispatched", "created_at": "2026-10-01T00:00:00+00:00"}, False),
        ({"status": "planned", "created_at": "not-a-date"}, False),
        ({"status": "planned"}, False),
    ],
)
def test_planned_is_stale_negative(row, expected):
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    assert rd.planned_is_stale(row, now=now) is expected


@pytest.mark.unit
def test_planned_is_stale_positive_and_boundary():
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    fresh = {"status": "planned", "created_at": (now - timedelta(minutes=5)).isoformat()}
    stale = {"status": "planned", "created_at": (now - timedelta(minutes=31)).isoformat()}
    assert rd.planned_is_stale(fresh, now=now) is False
    assert rd.planned_is_stale(stale, now=now) is True
    # naive 时间戳按 UTC 解读（DB 列 naive UTC 口径）
    naive = {"status": "planned", "created_at": (now - timedelta(hours=2)).replace(tzinfo=None).isoformat()}
    assert rd.planned_is_stale(naive, now=now) is True


@pytest.mark.unit
def test_planned_is_stale_uses_updated_at_and_ignores_run_id():
    """陈旧判定看 updated_at（reopen 刷新的探针时钟），run_id 不豁免（审查 F1）。"""
    now = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    # 重开行：created_at 很老但 updated_at 刚刷新（提交在途）→ 不判陈旧
    reopened = {
        "status": "planned",
        "run_id": "run_old_attempt",
        "created_at": (now - timedelta(days=3)).isoformat(),
        "updated_at": (now - timedelta(minutes=5)).isoformat(),
    }
    assert rd.planned_is_stale(reopened, now=now) is False
    # 重开后在提交前再次崩溃：updated_at 也老了 + 旧 run_id → 仍判陈旧（可自愈）
    reopened["updated_at"] = (now - timedelta(minutes=31)).isoformat()
    assert rd.planned_is_stale(reopened, now=now) is True


# ---------------------------------------------------------------------------
# 内存守卫
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_read_mem_available_gb(tmp_path):
    proc = tmp_path / "meminfo"
    proc.write_text("MemTotal:       65536000 kB\nMemAvailable:   52428800 kB\n", encoding="ascii")
    assert rd.read_mem_available_gb(str(proc)) == 50.0
    assert rd.read_mem_available_gb(str(tmp_path / "missing")) is None


@pytest.mark.unit
def test_mem_guard_low_and_ok(tmp_path):
    proc = tmp_path / "meminfo"
    proc.write_text("MemAvailable:   41943040 kB\n", encoding="ascii")  # 40 GB
    low = rd.mem_guard(min_gb=50, path=str(proc))
    assert low["ok"] is False and low["reason"] == "low_memory" and low["available_gb"] == 40.0
    proc.write_text("MemAvailable:   62914560 kB\n", encoding="ascii")  # 60 GB
    ok = rd.mem_guard(min_gb=50, path=str(proc))
    assert ok["ok"] is True and ok["available_gb"] == 60.0
    # 读不到 → fail-open（硬闸是单飞锁，本守卫只是缓冲）
    missing = rd.mem_guard(min_gb=50, path=str(tmp_path / "missing"))
    assert missing["ok"] is True and missing["reason"] == "meminfo_unavailable"


# ---------------------------------------------------------------------------
# alert_once：每日一次去重
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self):
        self.store: dict = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = (value, ex)
        return True


@pytest.mark.unit
def test_alert_once_dedups_per_reason_per_day_and_survives_redis_outage(monkeypatch):
    """同一天同一 reason 只发一条；不同 reason 互不遮蔽；Redis 挂了宁可多发。"""
    redis = _FakeRedis()
    sent: list[str] = []
    import backend.shared.qq_notify as qq

    monkeypatch.setattr(
        qq, "alert_async", lambda **kw: sent.append(str(kw.get("title") or ""))
    )

    assert rd.alert_once("busy", "T1", "c1", redis_client=redis) is True
    assert rd.alert_once("busy", "T1", "c2", redis_client=redis) is False  # 当日去重
    assert rd.alert_once("mem", "T2", "c3", redis_client=redis) is True  # 另一 reason 不受影响
    assert sent == ["T1", "T2"]
    # TTL 必须覆盖「同日补发」窗口（2 天），过期太早会在深夜重复喊
    assert all(ex == 2 * 24 * 3600 for _, ex in redis.store.values())

    class _Broken:
        def set(self, *a, **kw):
            raise RuntimeError("redis down")

    assert rd.alert_once("http", "T3", "c4", redis_client=_Broken()) is True
    assert sent[-1] == "T3"


# ---------------------------------------------------------------------------
# busy 探针
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_busy_holder_and_failure(monkeypatch):
    import backend.shared.training_singleflight as sf

    monkeypatch.setattr(sf, "get_holder", lambda client: "run_holder_1")
    busy = await rd.probe_busy(redis_client=object())
    assert busy["busy"] is True and busy["reason"] == "training_singleflight"
    assert busy["holder"] == "run_holder_1"

    def _boom(client):
        raise RuntimeError("redis down")

    monkeypatch.setattr(sf, "get_holder", _boom)
    failed = await rd.probe_busy(redis_client=object())
    assert failed["busy"] is True and failed["reason"] == "busy_probe_failed"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_busy_idle_and_active_row(monkeypatch):
    import backend.shared.training_singleflight as sf

    monkeypatch.setattr(sf, "get_holder", lambda client: None)

    async def _idle():
        return None

    monkeypatch.setattr(rd, "_query_active_training_row", _idle)
    idle = await rd.probe_busy(redis_client=object())
    assert idle == {"busy": False, "reason": "idle"}

    async def _active():
        return ("run_9", "running")

    monkeypatch.setattr(rd, "_query_active_training_row", _active)
    active = await rd.probe_busy(redis_client=object())
    assert active["busy"] is True and active["reason"] == "active_training_job"
    assert active["run_id"] == "run_9" and active["status"] == "running"


@pytest.mark.unit
def test_probe_engine_singleton_uses_nullpool(monkeypatch):
    """探针独立 NullPool 引擎：不碰共享池（池化连接跨短命 loop 复用必炸，审查 F3）。"""
    from sqlalchemy.pool import NullPool

    monkeypatch.setattr(rd, "_probe_engine", None)
    first = rd._get_probe_engine()
    assert first is rd._get_probe_engine()
    assert isinstance(first.pool, NullPool)


# ---------------------------------------------------------------------------
# execute_dispatch 主流程（IO 层全部打桩）
# ---------------------------------------------------------------------------


class _DispatchHarness:
    def __init__(
        self,
        monkeypatch,
        days,
        *,
        recipe=None,
        existing=None,
        submit_result=None,
        submit_exc=None,
    ):
        self.submit_calls: list[tuple] = []
        self.mark_failed_calls: list[tuple] = []
        self.submit_result = submit_result or {"runId": "run_dispatched_1"}
        self.submit_exc = submit_exc
        if recipe is not None:
            # 合成配方只存在于内存 —— 把 registry 装载口也打桩（真配方测试不传本参数）
            monkeypatch.setattr(rd, "load_recipe", lambda _rid: recipe)
        monkeypatch.setattr(rd, "_calendar_days", lambda market, today: days)
        monkeypatch.setattr(rd, "_factor_dates", lambda market, source: days)

        async def _probe(_client=None):
            return {"busy": False, "reason": "idle"}

        monkeypatch.setattr(rd, "probe_busy", _probe)

        async def _get_by_window(*_a, **_k):
            return existing

        monkeypatch.setattr(rd, "get_campaign_by_window", _get_by_window)

        async def _insert(**kwargs):
            return {"campaign_id": kwargs["campaign_id"], "status": "planned", "attempts": 0}

        monkeypatch.setattr(rd, "insert_campaign", _insert)

        async def _reopen(cid):
            return {"campaign_id": cid, "status": "planned", "attempts": 0}

        monkeypatch.setattr(rd, "reopen_campaign", _reopen)

        async def _mark_dispatched(cid, run_id):
            return {"campaign_id": cid, "run_id": run_id, "status": "dispatched", "attempts": 1}

        monkeypatch.setattr(rd, "mark_dispatched", _mark_dispatched)

        async def _mark_failed(cid, reason, detail=None):
            self.mark_failed_calls.append((cid, reason, detail))
            return True

        monkeypatch.setattr(rd, "mark_failed", _mark_failed)

        async def _submit(payload, background_tasks, user):
            self.submit_calls.append((payload, background_tasks, user))
            if self.submit_exc is not None:
                raise self.submit_exc
            return self.submit_result

        self.submit = _submit


def _weekdays(start: date, end: date) -> list[date]:
    days, cur = [], start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_dispatch_happy_path_payload_complete(monkeypatch, small_days):
    harness = _DispatchHarness(monkeypatch, small_days, recipe=_synthetic_recipe())
    out = await rd.execute_dispatch(
        market="CN",
        recipe_id="unittest_recipe",
        trigger="schedule",
        submit_fn=harness.submit,
        redis_client=object(),
        today=date(2026, 4, 13),
    )
    assert out["status"] == "dispatched" and out["run_id"] == "run_dispatched_1"
    assert out["campaign_id"] == "rc_cn_unittest_recipe_20260408"
    (payload, _bg, user), = harness.submit_calls
    # 六键 split 与窗口逐字一致
    assert payload["train_start"] == "2026-03-20" and payload["test_end"] == "2026-04-08"
    assert payload["valid_start"] == "2026-03-31" and payload["valid_end"] == "2026-04-02"
    assert payload["test_start"] == "2026-04-07"
    meta = payload["rolling_meta"]
    assert meta["campaign_id"] == out["campaign_id"]
    assert meta["anchor_date"] == "2026-04-08" and meta["purge_days"] == 2
    assert meta["dispatched_by"] == rd.DISPATCHED_BY_SCHEDULER
    assert payload["job_name"] == out["campaign_id"]
    assert user == rd.CUSTOM_USER


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_dispatch_dry_run_zero_side_effects(monkeypatch, small_days):
    harness = _DispatchHarness(monkeypatch, small_days, recipe=_synthetic_recipe())
    out = await rd.execute_dispatch(
        market="CN",
        recipe_id="unittest_recipe",
        dry_run=True,
        submit_fn=harness.submit,
        redis_client=object(),
        today=date(2026, 4, 13),
    )
    assert out["status"] == "dry_run" and out["ready"] is True
    assert out["plan"]["anchor_date"] == "2026-04-08"
    assert harness.submit_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_dispatch_busy_skips_without_submit(monkeypatch, small_days):
    harness = _DispatchHarness(monkeypatch, small_days, recipe=_synthetic_recipe())

    async def _probe(_client=None):
        return {"busy": True, "reason": "training_singleflight", "holder": "run_x"}

    monkeypatch.setattr(rd, "probe_busy", _probe)
    out = await rd.execute_dispatch(
        market="CN",
        recipe_id="unittest_recipe",
        submit_fn=harness.submit,
        redis_client=object(),
        today=date(2026, 4, 13),
    )
    assert out["status"] == "skipped" and out["reason"] == "busy"
    assert harness.submit_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_dispatch_duplicate_registered_never_resubmits(monkeypatch, small_days):
    harness = _DispatchHarness(
        monkeypatch,
        small_days,
        recipe=_synthetic_recipe(),
        existing={"status": "registered", "attempts": 1, "run_id": "run_old", "created_at": "2026-04-08T00:00:00+00:00"},
    )
    out = await rd.execute_dispatch(
        market="CN",
        recipe_id="unittest_recipe",
        submit_fn=harness.submit,
        redis_client=object(),
        today=date(2026, 4, 13),
    )
    assert out["status"] == "duplicate" and out["reason"] == "already_registered"
    assert harness.submit_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_dispatch_409_marks_busy_without_attempt(monkeypatch, small_days):
    from fastapi import HTTPException

    harness = _DispatchHarness(
        monkeypatch,
        small_days,
        recipe=_synthetic_recipe(),
        submit_exc=HTTPException(status_code=409, detail="训练进行中"),
    )
    out = await rd.execute_dispatch(
        market="CN",
        recipe_id="unittest_recipe",
        submit_fn=harness.submit,
        redis_client=object(),
        today=date(2026, 4, 13),
    )
    assert out["status"] == "skipped" and out["reason"] == "busy"
    assert harness.mark_failed_calls
    cid, reason, _detail = harness.mark_failed_calls[-1]
    assert reason == "busy_409" and cid == "rc_cn_unittest_recipe_20260408"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_dispatch_retries_stale_planned(monkeypatch, small_days):
    harness = _DispatchHarness(
        monkeypatch,
        small_days,
        recipe=_synthetic_recipe(),
        existing={
            "campaign_id": "rc_cn_unittest_recipe_20260408",
            "status": "planned",
            "attempts": 0,
            "run_id": None,
            "created_at": "2026-04-08T00:00:00+00:00",
        },
    )
    out = await rd.execute_dispatch(
        market="CN",
        recipe_id="unittest_recipe",
        submit_fn=harness.submit,
        redis_client=object(),
        today=date(2026, 4, 13),
    )
    # created_at 远早于 now（测试运行日）→ 判陈旧 → 重开重试
    assert out["status"] == "dispatched"
    assert len(harness.submit_calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_dispatch_retries_stale_planned_with_old_run_id(monkeypatch, small_days):
    """重开后在提交前再次崩溃（planned + 旧 run_id）同样重开补发（审查 F1）。

    旧判定要求「无 run_id」才算陈旧——这类行会永久卡死；现在只看 updated_at。
    """
    harness = _DispatchHarness(
        monkeypatch,
        small_days,
        recipe=_synthetic_recipe(),
        existing={
            "campaign_id": "rc_cn_unittest_recipe_20260408",
            "status": "planned",
            "attempts": 1,
            "run_id": "run_old_attempt",
            "created_at": "2026-04-01T00:00:00+00:00",
            "updated_at": "2026-04-01T00:00:00+00:00",
        },
    )
    out = await rd.execute_dispatch(
        market="CN",
        recipe_id="unittest_recipe",
        submit_fn=harness.submit,
        redis_client=object(),
        today=date(2026, 4, 13),
    )
    assert out["status"] == "dispatched"
    assert len(harness.submit_calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_dispatch_market_mismatch_raises(small_days):
    with pytest.raises(ValueError):
        await rd.execute_dispatch(
            market="US",
            recipe_id="cn_nativetft_base",
            dry_run=True,
            today=date(2026, 4, 13),
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_execute_dispatch_real_recipe_full_window_ready(monkeypatch):
    """真配方（CN/756/126/63/h=5）+ 足量日历 → 端到端就绪且注入完整。"""
    days = _weekdays(date(2020, 1, 1), date(2024, 9, 6))
    harness = _DispatchHarness(monkeypatch, days)
    out = await rd.execute_dispatch(
        market="CN",
        recipe_id="cn_nativetft_base",
        submit_fn=harness.submit,
        redis_client=object(),
        today=date(2024, 9, 9),
    )
    assert out["status"] == "dispatched"
    (payload, _bg, _user), = harness.submit_calls
    recipe = load_recipe("cn_nativetft_base")
    assert len(payload["features"]) == len(recipe.payload["features"])
    assert payload["model_type"] == "nativetft"
    meta = payload["rolling_meta"]
    assert meta["purge_days"] == 6 and meta["recipe_hash"]
    assert payload["deploy_to_production"] is False


# ---------------------------------------------------------------------------
# execute_dispatch 跳过路径（skipped 语义 = 调度器「本轮不记账、下一 tick 重试」
# 的依据；dry_run 变体必须零告警零副作用——CLI 演练会跑它）
# ---------------------------------------------------------------------------


def _alert_spy(monkeypatch) -> list[str]:
    import backend.shared.qq_notify as qq

    sent: list[str] = []
    monkeypatch.setattr(
        qq, "alert_async", lambda **kw: sent.append(str(kw.get("title") or ""))
    )
    return sent


def _call(harness, **overrides):
    kwargs = {
        "market": "CN",
        "recipe_id": "unittest_recipe",
        "submit_fn": harness.submit,
        "redis_client": _FakeRedis(),
        "today": date(2026, 4, 13),
    }
    kwargs.update(overrides)
    return rd.execute_dispatch(**kwargs)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_calendar_unavailable_skips_with_alert_dry_run_quiet(monkeypatch, small_days):
    harness = _DispatchHarness(monkeypatch, small_days, recipe=_synthetic_recipe())
    monkeypatch.setattr(rd, "_calendar_days", lambda market, today: None)
    sent = _alert_spy(monkeypatch)

    out = await _call(harness)
    assert out["status"] == "skipped" and out["reason"] == "calendar_unavailable"
    assert harness.submit_calls == []
    assert sent and "日历" in sent[0]

    sent.clear()
    out = await _call(harness, dry_run=True)
    assert out == {"status": "dry_run", "ready": False, "reason": "calendar_unavailable"}
    assert sent == [] and harness.submit_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_factor_source_unreadable_skips_with_alert_dry_run_quiet(monkeypatch, small_days):
    harness = _DispatchHarness(monkeypatch, small_days, recipe=_synthetic_recipe())

    def _boom(market, source):
        raise RuntimeError("分区表损坏")

    monkeypatch.setattr(rd, "_factor_dates", _boom)
    sent = _alert_spy(monkeypatch)

    out = await _call(harness)
    assert out["status"] == "skipped" and out["reason"] == "factor_source_unavailable"
    assert "分区表损坏" in str(out["detail"])
    assert sent and "因子源" in sent[0]

    sent.clear()
    out = await _call(harness, dry_run=True)
    assert out["status"] == "dry_run" and out["reason"] == "factor_source_unavailable"
    assert sent == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_data_lag_skips_with_alert_and_keeps_detail(monkeypatch, small_days):
    harness = _DispatchHarness(monkeypatch, small_days, recipe=_synthetic_recipe())
    stale = small_days[:-3]  # 因子分区滞后到 04-07
    monkeypatch.setattr(rd, "_factor_dates", lambda market, source: stale)
    sent = _alert_spy(monkeypatch)

    out = await _call(harness)
    assert out["status"] == "skipped" and out["reason"] == "data_lag"
    assert out["detail"]["factor_max_date"] == "2026-04-07"
    assert harness.submit_calls == [] and sent

    # dry-run 变体：同一裁决但不告警，detail 透传给演练人
    sent.clear()
    out = await _call(harness, dry_run=True)
    assert out["status"] == "dry_run" and out["ready"] is False
    assert out["reason"] == "data_lag" and out["detail"]["factor_max_date"] == "2026-04-07"
    assert sent == []


# ---------------------------------------------------------------------------
# 竞态与提交失败（保护「绝不二次提交」「慢台账要能对账」两条铁律）
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_insert_race_falls_back_to_duplicate(monkeypatch, small_days):
    """两个 tick 同窗并发：唯一键挡住 insert → 以胜者行回 duplicate，不重提。"""
    harness = _DispatchHarness(monkeypatch, small_days, recipe=_synthetic_recipe())

    async def _insert_none(**_k):
        return None

    monkeypatch.setattr(rd, "insert_campaign", _insert_none)
    calls = {"n": 0}

    async def _get_by_window(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # 首次查询无记录 → 本 tick 走 create
        return {"status": "dispatched", "run_id": "run_concurrent", "attempts": 1}

    monkeypatch.setattr(rd, "get_campaign_by_window", _get_by_window)

    out = await _call(harness)
    assert out["status"] == "duplicate" and out["reason"] == "in_flight"
    assert out["run_id"] == "run_concurrent"
    assert harness.submit_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_reopen_race_falls_back_to_duplicate(monkeypatch, small_days):
    harness = _DispatchHarness(
        monkeypatch,
        small_days,
        recipe=_synthetic_recipe(),
        existing={
            "campaign_id": "rc_cn_unittest_recipe_20260408",
            "status": "failed",
            "attempts": 0,
            "run_id": None,
            "created_at": "2026-04-08T00:00:00+00:00",
        },
    )

    async def _reopen_none(_cid):
        return None

    async def _latest(_cid):
        return {"status": "dispatched", "run_id": "run_late", "attempts": 1}

    monkeypatch.setattr(rd, "reopen_campaign", _reopen_none)
    monkeypatch.setattr(rd, "get_campaign", _latest)

    out = await _call(harness)
    assert out["status"] == "duplicate" and out["reason"] == "race:in_flight"
    assert out["run_id"] == "run_late"
    assert harness.submit_calls == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_failure_marks_failed_and_reraises(monkeypatch, small_days):
    harness = _DispatchHarness(
        monkeypatch,
        small_days,
        recipe=_synthetic_recipe(),
        submit_exc=RuntimeError("submit boom"),
    )
    with pytest.raises(RuntimeError, match="submit boom"):
        await _call(harness)
    cid, reason, detail = harness.mark_failed_calls[-1]
    assert cid == "rc_cn_unittest_recipe_20260408" and reason == "submit_failed"
    assert "submit boom" in str(detail)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_submit_without_run_id_marks_failed_and_raises(monkeypatch, small_days):
    harness = _DispatchHarness(
        monkeypatch,
        small_days,
        recipe=_synthetic_recipe(),
        submit_result={"no_run": True},  # 缺 runId（空 dict 会被 harness 当缺省）
    )
    with pytest.raises(RuntimeError, match="未返回 runId"):
        await _call(harness)
    _cid, reason, _detail = harness.mark_failed_calls[-1]
    assert reason == "submit_no_run_id"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_mark_dispatched_race_logs_but_returns_dispatched(monkeypatch, small_days):
    """提交成功但状态未落（极端竞态）：不吞不炸，run 已在跑——返回 dispatched。"""
    harness = _DispatchHarness(monkeypatch, small_days, recipe=_synthetic_recipe())

    async def _mark_none(_cid, _run_id):
        return None

    monkeypatch.setattr(rd, "mark_dispatched", _mark_none)
    out = await _call(harness)
    assert out["status"] == "dispatched"
    assert out["run_id"] == "run_dispatched_1"
    assert out["attempts"] is None  # 竞态行未回收 → attempts 缺省（对账面可见）


# ---------------------------------------------------------------------------
# 边界薄封装补齐
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_redis_factory_pins_socket_timeout():
    client = rd._redis()
    assert client.connection_pool.connection_kwargs.get("socket_timeout") == 3


@pytest.mark.unit
def test_calendar_days_live_calendar_integration():
    days = rd._calendar_days("CN", date(2026, 10, 8))
    assert days, "日历覆盖应包含 2026-10 前的回看窗"
    assert max(days) <= date(2026, 10, 8)


@pytest.mark.unit
def test_compute_plan_calendar_without_completed_session():
    result = rd.compute_plan(
        _synthetic_recipe(),
        calendar_days=[date(2026, 4, 13), date(2026, 4, 14)],
        factor_dates=[date(2026, 4, 14)],
        today=date(2026, 4, 13),
    )
    assert result.ready is False and result.reason == "calendar_no_completed_session"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_probe_busy_pg_failure_is_conservatively_busy(monkeypatch):
    import backend.shared.training_singleflight as sf

    monkeypatch.setattr(sf, "get_holder", lambda client: None)

    async def _boom():
        raise RuntimeError("pg down")

    monkeypatch.setattr(rd, "_query_active_training_row", _boom)
    out = await rd.probe_busy(redis_client=object())
    assert out["busy"] is True and out["reason"] == "busy_probe_failed"


@pytest.mark.unit
def test_alert_once_send_failure_returns_false(monkeypatch):
    import backend.shared.qq_notify as qq

    def _boom(**kw):
        raise RuntimeError("qq down")

    monkeypatch.setattr(qq, "alert_async", _boom)
    assert rd.alert_once("http", "T", "c", redis_client=_FakeRedis()) is False


@pytest.mark.unit
def test_read_mem_available_gb_file_without_field(tmp_path):
    proc = tmp_path / "meminfo"
    proc.write_text("MemTotal:       65536000 kB\n", encoding="ascii")
    assert rd.read_mem_available_gb(str(proc)) is None


@pytest.mark.unit
def test_planned_is_stale_accepts_naive_now():
    """naive 参考时刻按 UTC 解读（DB 列 naive UTC 口径的姊妹修正）。"""
    naive_now = datetime(2026, 10, 1, 12, 0)
    row = {"status": "planned", "created_at": "2026-10-01T09:00:00+00:00"}
    assert rd.planned_is_stale(row, now=naive_now) is True
