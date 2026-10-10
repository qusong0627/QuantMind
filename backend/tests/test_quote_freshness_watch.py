"""行情快照数据心跳监视测试：判活分级 / 告警文案 / 投递路由 / 接线断言。

防回退要点（审计 M8）：
- 判活 = **数据写入年龄**（freshness 唯一谓词），不是循环在不在跑；
- 读错 / 热集空 → 硬失败（fail-closed，不把「读不到」当健康）；
- 连续 3 次硬失败才报断流；stale 走软失败宽阈值（6 次）；
- 恢复事件 force 推 QQ（与桥健康监视同闭环纪律）；
- 采样走线程（不得阻塞交易事件循环）；
- 接线断言：注册表 JobSpec + 心跳字符串 + status 端点暴露段。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.services.live_trading.services.bridge_health_watch import (
    EVENT_DOWN,
    EVENT_RECOVERED,
    VERDICT_HARD,
    VERDICT_OK,
    VERDICT_SOFT,
    BridgeHealthTracker,
    HealthEvent,
)
from backend.services.live_trading.services.quote_freshness_watch import (
    build_quote_alert,
    classify_sample,
    notify_quote_event,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _sample(
    *,
    level: str = "fresh",
    age: float = 3.0,
    requested: int = 120,
    missing: int = 0,
    dominant: str = "tdx_bridge",
    error: str | None = None,
) -> dict:
    return {
        "requested": requested,
        "missing": missing,
        "dominant": dominant,
        "dominant_label": dominant,
        "level": level,
        "newest_age_sec": age,
        "sources": [],
        "as_of": "2026-10-10T10:00:00+08:00",
        "error": error,
    }


# ── 判活分级（纯函数）────────────────────────────────────────────────────


@pytest.mark.unit
def test_fresh_sample_is_ok():
    verdict, detail = classify_sample(_sample(level="fresh", age=3.0))
    assert verdict == VERDICT_OK and detail == ""


@pytest.mark.unit
def test_stale_sample_is_soft_degradation():
    verdict, detail = classify_sample(_sample(level="stale", age=120.0))
    assert verdict == VERDICT_SOFT and "120s" in detail and "降级" in detail


@pytest.mark.unit
def test_unavailable_sample_is_hard():
    verdict, detail = classify_sample(_sample(level="unavailable", age=900.0))
    assert verdict == VERDICT_HARD and "unavailable" in detail


@pytest.mark.unit
def test_missing_age_is_hard_fail_closed():
    """无任何样本（newest_age_sec=None）→ 硬失败，绝不把「读不到」当健康。"""
    verdict, _ = classify_sample(_sample(level="unavailable", age=None, missing=120))
    assert verdict == VERDICT_HARD


@pytest.mark.unit
def test_read_error_is_hard():
    verdict, detail = classify_sample(_sample(error="redis down", level="fresh"))
    assert verdict == VERDICT_HARD and "采样读取失败" in detail


@pytest.mark.unit
def test_empty_universe_is_hard_not_ok():
    """热集为空（requested=0）→ 硬失败：没有可判活的数据 ≠ 数据健康。"""
    verdict, detail = classify_sample(_sample(requested=0, missing=0, level="fresh"))
    assert verdict == VERDICT_HARD and "热集为空" in detail


@pytest.mark.unit
def test_unknown_level_fails_closed_as_hard():
    verdict, _ = classify_sample(_sample(level="???"))
    assert verdict == VERDICT_HARD


# ── 告警文案（纯函数）────────────────────────────────────────────────────


def _down_event(now: float = 1000.0) -> HealthEvent:
    tracker = BridgeHealthTracker(hard_threshold=3)
    event = None
    for i in range(3):
        event = tracker.record(
            VERDICT_HARD, detail="最新快照年龄 900s", now=now + 60 * i
        )
    assert event is not None and event.kind == EVENT_DOWN
    return event


@pytest.mark.unit
def test_down_alert_text_has_sample_and_hint():
    event = _down_event()
    title, content, level = build_quote_alert(
        event, sample=_sample(level="unavailable", age=900.0, missing=120), now=1200.0
    )
    assert title == "行情快照断流（market:snapshot）" and level == "error"
    assert "900s" in content
    assert "采样: 0/120 只有快照" in content
    assert "/tdx/quote-feed/status" in content


@pytest.mark.unit
def test_recovered_alert_carries_duration_and_success_level():
    tracker = BridgeHealthTracker(hard_threshold=3)
    for i in range(3):
        tracker.record(VERDICT_HARD, now=100.0 + 60 * i)
    recovered = tracker.record(VERDICT_OK, now=400.0)
    assert recovered is not None and recovered.kind == EVENT_RECOVERED

    title, content, level = build_quote_alert(recovered, now=400.0)

    assert title == "行情快照已恢复" and level == "success"
    assert "5 分 0 秒" in content


@pytest.mark.unit
def test_repeat_alert_marks_still_down():
    tracker = BridgeHealthTracker(hard_threshold=3, repeat_s=300.0)
    for i in range(3):
        tracker.record(VERDICT_HARD, now=100.0 + 60 * i)  # 判定点 220
    again = tracker.record(VERDICT_HARD, now=600.0)  # 距上条 380s ≥ repeat 300s
    assert again is not None and again.repeat is True

    title, content, _ = build_quote_alert(again, now=600.0)

    assert title == "行情快照仍处于断流"
    assert "已持续" in content


@pytest.mark.unit
def test_soft_run_alerts_at_wider_threshold():
    """stale 连击：5 次不报（既有 60s 定期快照在途），6 次报。"""
    tracker = BridgeHealthTracker(hard_threshold=3, soft_threshold=6)
    for i in range(5):
        assert tracker.record(VERDICT_SOFT, now=100.0 + i) is None
    event = tracker.record(VERDICT_SOFT, now=200.0)
    assert event is not None and event.kind == EVENT_DOWN


# ── 投递路由 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_down_event_routes_to_admins_data_quality(monkeypatch):
    from backend.shared import notification_publisher as np

    seen = {}

    async def _fake_fanout(**kwargs):
        seen.update(kwargs)
        return (1, 1)

    monkeypatch.setattr(np, "publish_notification_to_admins_async", _fake_fanout)
    qq_calls = []
    monkeypatch.setattr(
        "backend.shared.qq_notify.alert_async",
        lambda **kw: qq_calls.append(kw) or True,
    )

    sent = await notify_quote_event(_down_event())

    assert sent is True
    assert seen["type"] == "data_quality" and seen["level"] == "error"
    assert seen["title"] == "行情快照断流（market:snapshot）"
    assert qq_calls == [], "断流事件经发布器 QQ 旁路外发，不得重复直发"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recovered_event_force_pushes_qq(monkeypatch):
    from backend.shared import notification_publisher as np

    async def _fake_fanout(**kwargs):
        return (1, 1)

    monkeypatch.setattr(np, "publish_notification_to_admins_async", _fake_fanout)
    qq_calls = []
    monkeypatch.setattr(
        "backend.shared.qq_notify.alert_async",
        lambda **kw: qq_calls.append(kw) or True,
    )
    tracker = BridgeHealthTracker(hard_threshold=3)
    for i in range(3):
        tracker.record(VERDICT_HARD, now=100.0 + 60 * i)
    recovered = tracker.record(VERDICT_OK, now=400.0)

    await notify_quote_event(recovered)

    assert len(qq_calls) == 1
    assert qq_calls[0]["level"] == "success" and qq_calls[0]["force"] is True
    assert qq_calls[0]["title"] == "行情快照已恢复"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delivery_failure_never_escapes(monkeypatch):
    from backend.shared import notification_publisher as np

    async def _boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(np, "publish_notification_to_admins_async", _boom)

    assert await notify_quote_event(_down_event()) is False


# ── 探测节拍（_watch_tick：采样 → 判定 → 状态机）─────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_feeds_tracker_and_alerts_on_third_hard(monkeypatch):
    from backend.services.live_trading.services import quote_freshness_watch as watch

    broken = _sample(level="unavailable", age=900.0, missing=120)
    tracker = BridgeHealthTracker(hard_threshold=3)

    for _ in range(2):
        event, _ = await watch._watch_tick(tracker, sampler=lambda: broken)
        assert event is None, "未到阈值不得报"
    event, sample = await watch._watch_tick(tracker, sampler=lambda: broken)

    assert event is not None and event.kind == EVENT_DOWN
    assert sample["missing"] == 120
    assert watch.quote_freshness_status["verdict"] == VERDICT_HARD
    assert watch.quote_freshness_status["level"] == "unavailable"
    assert watch.quote_freshness_status["requested"] == 120
    assert watch.quote_freshness_status["tracker"]["down"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tick_sampler_exception_counts_as_hard_failure():
    from backend.services.live_trading.services import quote_freshness_watch as watch

    def _boom():
        raise RuntimeError("redis down")

    tracker = BridgeHealthTracker(hard_threshold=3)
    for _ in range(3):
        event, sample = await watch._watch_tick(tracker, sampler=_boom)

    assert event is not None and event.kind == EVENT_DOWN
    assert "redis down" in event.detail
    assert watch.quote_freshness_status["last_error"] == "redis down"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sampler_runs_off_the_event_loop(monkeypatch):
    """采样是同步 Redis 读——必须在别的线程跑（monkeypatch 记录线程身份）。"""
    import threading

    from backend.services.live_trading.services import quote_freshness_watch as watch

    seen: list[int] = []

    def _sampler():
        seen.append(threading.get_ident())
        return _sample()

    loop_ident = threading.get_ident()
    tracker = BridgeHealthTracker(hard_threshold=3)
    await watch._watch_tick(tracker, sampler=_sampler)

    assert seen and seen[0] != loop_ident


# ── 接线断言（注册表 / 主入口 / status 端点）─────────────────────────────


@pytest.mark.unit
def test_registry_has_jobspec_and_heartbeat():
    from backend.shared.scheduler_registry import JOBS, heartbeat_key

    spec = next(j for j in JOBS if j.key == "quote_freshness_watch")
    assert spec.owner == "trade" and spec.kind == "worker"
    assert (
        spec.switch_env == "QM_QUOTE_WATCH_ENABLED" and spec.switch_default_on is True
    )
    assert spec.heartbeat_ttl and spec.heartbeat_ttl >= 60
    assert heartbeat_key(spec.key) == "qm:sched:hb:quote_freshness_watch"


@pytest.mark.unit
def test_trade_main_wires_and_shuts_down_task():
    src = (_PROJECT_ROOT / "backend/services/trade/main.py").read_text(encoding="utf-8")
    assert "run_quote_freshness_watch_task" in src, "主入口未接监视任务"
    assert '"quote-freshness-watch"' in src, "任务缺名（停机日志对不上）"
    assert "QM_QUOTE_WATCH_ENABLED" in src, "缺开关闸门"
    # 停机元组（唯一 `for task in (` 循环）：漏进元组的任务 cancel 不到，挂起停机
    shutdown_block = src.split("for task in (", 1)[1].split("):", 1)[0]
    assert "quote_freshness_watch_task," in shutdown_block, "停机未取消（挂起停机）"


@pytest.mark.unit
def test_status_endpoint_exposes_quote_freshness_segment():
    src = (
        _PROJECT_ROOT / "backend/services/trade/routers/tdx_quote_feed.py"
    ).read_text(encoding="utf-8")
    assert "quote_freshness_status" in src, "status 端点未暴露行情心跳段"
    assert 'status["quote_freshness"]' in src
