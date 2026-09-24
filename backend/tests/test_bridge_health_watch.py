"""桥账户通道健康监视测试：状态机边沿 / 分级阈值 / 结论识别 / 投递路由。

防回退要点：
- 硬失败 3 次才报（30s 节拍 ≈90s），1~2 次不得报（瞬时抖动不吵人）；
- 软失败（空账户）阈值更宽（开盘前后短暂空账户是既有合法场景）；
- 只在边沿投递：掉线一次、恢复一次，持续掉线按 repeat_s 复提醒；
- 恢复事件必须 force 推 QQ（QQ 旁路等级过滤会挡 success，掉线闭环要回手机）；
- 账户同步的失败不一定抛异常（skipped/空账户/success=False），必须全部识别。
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
    build_alert,
    classify_sync_result,
    notify_bridge_event,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


# ── 结论识别 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_classify_recognizes_empty_account_as_soft():
    verdict, detail = classify_sync_result(
        {"success": True, "skipped": True, "reason": "empty_account"}
    )
    assert verdict == VERDICT_SOFT and "空" in detail


@pytest.mark.unit
def test_classify_recognizes_success_false_as_hard():
    verdict, detail = classify_sync_result(
        {"success": False, "error": "TDX_BRIDGE_URL/TOKEN 未配置"}
    )
    assert verdict == VERDICT_HARD and "未配置" in detail


@pytest.mark.unit
def test_classify_recognizes_normal_sync_as_ok():
    assert (
        classify_sync_result({"success": True, "total_asset": 918524.38})[0]
        == VERDICT_OK
    )


@pytest.mark.unit
def test_classify_non_mapping_is_hard():
    assert classify_sync_result(None)[0] == VERDICT_HARD
    assert classify_sync_result("boom")[0] == VERDICT_HARD


# ── 状态机：阈值与边沿 ───────────────────────────────────────────────────


@pytest.mark.unit
def test_hard_failures_alert_only_after_threshold_and_only_once():
    tracker = BridgeHealthTracker()
    assert tracker.record(VERDICT_HARD, detail="连接超时", now=100.0) is None
    assert tracker.record(VERDICT_HARD, detail="连接超时", now=130.0) is None
    event = tracker.record(VERDICT_HARD, detail="连接超时", now=160.0)
    assert event is not None and event.kind == EVENT_DOWN and not event.repeat
    assert event.consecutive == 3
    assert event.down_since == 100.0, "掉线起点应是本轮第一次失败，不是判定时刻"
    assert tracker.is_down is True
    # 仍然失败但未到复提醒间隔 → 不再投递
    assert tracker.record(VERDICT_HARD, detail="连接超时", now=190.0) is None


@pytest.mark.unit
def test_two_failures_then_recovery_never_alerts():
    tracker = BridgeHealthTracker()
    assert tracker.record(VERDICT_HARD, now=100.0) is None
    assert tracker.record(VERDICT_HARD, now=130.0) is None
    assert tracker.record(VERDICT_OK, now=160.0) is None
    assert tracker.is_down is False


@pytest.mark.unit
def test_recovery_event_carries_outage_duration():
    tracker = BridgeHealthTracker()
    for i in range(3):
        tracker.record(VERDICT_HARD, now=100.0 + 30 * i)
    event = tracker.record(VERDICT_OK, now=280.0)
    assert event is not None and event.kind == EVENT_RECOVERED
    assert event.down_for_s == pytest.approx(180.0)
    assert tracker.record(VERDICT_OK, now=310.0) is None, "恢复只报一条"


@pytest.mark.unit
def test_down_state_repeats_after_repeat_interval():
    tracker = BridgeHealthTracker(repeat_s=300.0)
    for i in range(3):
        tracker.record(VERDICT_HARD, detail="桥返回 HTTP 502", now=100.0 + 30 * i)
    assert tracker.record(VERDICT_HARD, now=200.0) is None
    again = tracker.record(VERDICT_HARD, now=470.0)
    assert again is not None and again.kind == EVENT_DOWN and again.repeat is True
    assert again.down_for_s == pytest.approx(370.0)


@pytest.mark.unit
def test_soft_failures_need_wider_run_and_interleave_resets():
    tracker = BridgeHealthTracker()
    for i in range(5):
        assert tracker.record(VERDICT_SOFT, now=100.0 + i) is None, "空账户 5 次内不报"
    assert tracker.record(VERDICT_HARD, now=200.0) is None, "硬软交替互相打断计数"
    for i in range(9):
        assert tracker.record(VERDICT_SOFT, now=300.0 + i) is None
    event = tracker.record(VERDICT_SOFT, now=400.0)
    assert event is not None and event.kind == EVENT_DOWN and event.consecutive == 10
    assert event.down_since == 300.0, "软失败轮起点取该轮第一次"


@pytest.mark.unit
def test_unknown_verdict_fails_closed_as_hard():
    tracker = BridgeHealthTracker()
    tracker.record("???", now=1.0)
    tracker.record("???", now=2.0)
    event = tracker.record("???", now=3.0)
    assert event is not None and event.kind == EVENT_DOWN
    assert "???" in event.detail


# ── 告警文案 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_build_alert_down_and_recovered_texts():
    tracker = BridgeHealthTracker()
    down = None
    for i in range(3):
        down = tracker.record(
            VERDICT_HARD, detail="桥返回 HTTP 502", now=100.0 + 30 * i
        )
    assert down is not None and down.kind == EVENT_DOWN
    title, content, level = build_alert(
        down, bridge_url="http://192.0.2.13:8550", interval_seconds=30, now=160.0
    )
    assert title == "通达信桥账户通道掉线" and level == "error"
    assert "桥返回 HTTP 502" in content and "192.0.2.13:8550" in content

    recovered = tracker.record(VERDICT_OK, now=500.0)
    r_title, r_content, r_level = build_alert(recovered, now=500.0)
    assert r_title == "通达信桥已恢复" and r_level == "success"
    assert "6 分 40 秒" in r_content, "掉线时长按首次失败（100）到恢复（500）计"


# ── 投递路由 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_down_event_routes_to_admins_and_never_forces_qq(monkeypatch):
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
    tracker = BridgeHealthTracker()
    for i in range(3):
        event = tracker.record(VERDICT_HARD, detail="连接被拒绝", now=100.0 + 30 * i)
    assert await notify_bridge_event(event, bridge_url="http://127.0.0.1:8550") is True
    assert seen["type"] == "health" and seen["level"] == "error"
    assert "连接被拒绝" in seen["content"]
    assert qq_calls == [], "掉线事件经发布器 QQ 旁路外发，不得重复直发"


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
    tracker = BridgeHealthTracker()
    for i in range(3):
        tracker.record(VERDICT_HARD, now=100.0 + 30 * i)
    recovered = tracker.record(VERDICT_OK, now=400.0)
    assert await notify_bridge_event(recovered) is True
    assert len(qq_calls) == 1
    assert qq_calls[0]["level"] == "success" and qq_calls[0]["force"] is True
    assert qq_calls[0]["title"] == "通达信桥已恢复"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delivery_failure_never_escapes(monkeypatch):
    from backend.shared import notification_publisher as np

    async def _boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(np, "publish_notification_to_admins_async", _boom)
    tracker = BridgeHealthTracker()
    for i in range(3):
        event = tracker.record(VERDICT_HARD, now=100.0 + 30 * i)
    assert await notify_bridge_event(event) is False


# ── 接线断言 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_account_sync_task_feeds_tracker_and_notifies():
    src = (
        _PROJECT_ROOT
        / "backend/services/live_trading/services/tdx_account_sync_task.py"
    ).read_text(encoding="utf-8")
    assert "BridgeHealthTracker()" in src, "同步任务未接健康状态机"
    assert "classify_sync_result(result)" in src, "同步结论未分类"
    assert "await notify_bridge_event(" in src, "事件未投递"
    assert "tracker.record(verdict" in src, "结论未喂入状态机"


@pytest.mark.unit
def test_sync_exception_path_counts_as_hard_failure():
    """桥不可达时 sync_account_to_pg 抛异常——该路径必须计硬失败。

    实现口径：每轮先把 verdict 默认成 VERDICT_HARD，异常直接沿用默认值，
    只有拿到返回值才用 classify 覆盖。故默认赋值必须先于 try。
    """
    src = (
        _PROJECT_ROOT
        / "backend/services/live_trading/services/tdx_account_sync_task.py"
    ).read_text(encoding="utf-8")
    body = src.split("while True:", 1)[1]
    default_pos = body.find('verdict, detail = VERDICT_HARD, ""')
    try_pos = body.find("try:")
    record_pos = body.find("tracker.record(")
    assert 0 <= default_pos < try_pos < record_pos


@pytest.mark.unit
def test_asyncio_cancellation_not_swallowed_by_health_path():
    """CancelledError 必须先于 Exception 分支（否则停机时被当失败吞掉）。"""
    src = (
        _PROJECT_ROOT
        / "backend/services/live_trading/services/tdx_account_sync_task.py"
    ).read_text(encoding="utf-8")
    body = src.split("while True:", 1)[1]
    assert body.find("except asyncio.CancelledError:") < body.find(
        "except Exception as exc:"
    )


@pytest.mark.unit
def test_sentinel_fanout_delegates_to_shared_helper():
    """sentinel 与桥监视共用同一管理员 fanout（受众规则单一出处）。"""
    src = (
        _PROJECT_ROOT / "backend/services/trade/services/sentinel_alert_service.py"
    ).read_text(encoding="utf-8")
    body = src.split("def _default_notify(", 1)[1]
    assert "publish_notification_to_admins(" in body
    assert "FROM users WHERE is_admin" not in body, "管理员 fanout SQL 不应各自复制"


@pytest.mark.unit
def test_event_loop_stays_responsive_during_notify(monkeypatch):
    """投递必须在线程/非阻塞路径（监视循环与交易任务共用事件循环）。"""

    async def _run():
        order = []

        async def _fake_fanout(**kwargs):
            order.append("fanout")
            await asyncio.sleep(0)
            return (1, 1)

        from backend.shared import notification_publisher as np

        monkeypatch.setattr(np, "publish_notification_to_admins_async", _fake_fanout)
        ticker_ran = asyncio.Event()

        async def _ticker():
            await asyncio.sleep(0)
            ticker_ran.set()

        tracker = BridgeHealthTracker()
        for i in range(3):
            event = tracker.record(VERDICT_HARD, now=100.0 + 30 * i)
        await asyncio.gather(notify_bridge_event(event), _ticker())
        assert ticker_ran.is_set() and order == ["fanout"]

    asyncio.run(_run())
