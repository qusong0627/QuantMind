"""QMT 管道健康监视与超时委托核对（纯逻辑测试）。

钉住三件事：
- 指标判据：队列积压/持仓推送停滞/委托回报停滞 各自触发；**读不到指标不判故障**
  （桥刚重启时指标未写入，误报会淹没真实故障）。
- 边沿触发：连续 N 次异常才报「转异常」，恢复只报一次，持续异常按 repeat_s 复报。
- 超时委托核对：事件流出现 remark 即销账；超过 stale_after_s 未见 → 进告警清单；
  条目是坏 JSON 也不能把整轮核对带崩。
"""

from __future__ import annotations

import json

from backend.services.live_trading.services import qmt_pipe_guard as g


def test_evaluate_pipe_metrics_healthy():
    metrics = {"queue_len": "0", "pos_age_s": "1.2", "order_age_s": "0.5"}
    assert g.evaluate_pipe_metrics(metrics) == []


def test_evaluate_pipe_metrics_empty_is_not_failure():
    assert g.evaluate_pipe_metrics({}) == []
    assert g.evaluate_pipe_metrics(None) == []


def test_evaluate_pipe_metrics_queue_backlog():
    issues = g.evaluate_pipe_metrics({"queue_len": "3", "pos_age_s": "1"})
    assert len(issues) == 1 and "积压" in issues[0]


def test_evaluate_pipe_metrics_stalled_positions_and_orders():
    issues = g.evaluate_pipe_metrics(
        {"queue_len": "0", "pos_age_s": "120", "order_age_s": "61"}
    )
    assert len(issues) == 2
    assert any("持仓推送" in x for x in issues)
    assert any("委托回报" in x for x in issues)


def test_evaluate_pipe_metrics_ignores_bad_numbers():
    issues = g.evaluate_pipe_metrics(
        {"queue_len": "abc", "pos_age_s": None, "order_age_s": "x"}
    )
    assert issues == []


def test_tracker_requires_consecutive_threshold():
    tracker = g.PipeHealthTracker(hard_threshold=2)
    assert tracker.feed(["x"], now=100.0) is None
    event = tracker.feed(["x"], now=101.0)
    assert event is not None and event.kind == g.EVENT_DOWN
    assert event.consecutive == 2


def test_tracker_recovers_once():
    tracker = g.PipeHealthTracker(hard_threshold=1)
    tracker.feed(["x"], now=100.0)
    event = tracker.feed([], now=130.0)
    assert event is not None and event.kind == g.EVENT_RECOVERED
    assert event.down_for_s == 30.0
    assert tracker.feed([], now=160.0) is None


def test_tracker_repeat_alert():
    tracker = g.PipeHealthTracker(hard_threshold=1, repeat_s=60.0)
    tracker.feed(["x"], now=100.0)
    assert tracker.feed(["x"], now=130.0) is None
    repeat = tracker.feed(["x"], now=161.0)
    assert repeat is not None and repeat.repeat and repeat.kind == g.EVENT_DOWN


def test_tracker_transient_blip_does_not_alert():
    tracker = g.PipeHealthTracker(hard_threshold=2)
    assert tracker.feed(["x"], now=1.0) is None
    assert tracker.feed([], now=2.0) is None
    assert not tracker.is_down


def test_reconcile_unverified_resolves_and_flags_stale():
    unverified = {
        "qm-a": json.dumps({"ts": 1000.0, "symbol": "600036.SH"}),
        "qm-b": {"ts": 1000.0},
        "qm-c": json.dumps({"ts": 1000.0}),
    }
    resolved, stale = g.reconcile_unverified(
        unverified, {"qm-a"}, now=1700.0, stale_after_s=600.0
    )
    assert resolved == ["qm-a"]
    assert {x["remark"] for x in stale} == {"qm-b", "qm-c"}


def test_reconcile_unverified_bad_payload_does_not_crash():
    resolved, stale = g.reconcile_unverified(
        {"qm-x": "not-json"}, set(), now=10.0, stale_after_s=0
    )
    assert resolved == [] and stale == []


def test_read_order_event_remarks_extracts_fields():
    rows = [
        json.dumps({"row": {"user_order_id": "qm-1", "remark": "qm-2"}}),
        json.dumps({"row": {"order_remark": "qm-3"}}),
        "not-json",
        json.dumps({"row": {}}),
    ]
    assert g.read_order_event_remarks(rows) == {"qm-1", "qm-2", "qm-3"}


def test_build_alert_titles():
    down = g.PipeEvent(g.EVENT_DOWN, ("队列积压 5",), 2, 0.0)
    title, content, level = g.build_alert(down, account="40327478")
    assert title == "QMT 管道异常" and level == "error" and "积压" in content
    recovered = g.PipeEvent(g.EVENT_RECOVERED, (), 0, 42.0)
    title, _, level = g.build_alert(recovered)
    assert title == "QMT 管道已恢复" and level == "success"
