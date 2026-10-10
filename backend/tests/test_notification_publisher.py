"""通知发布器测试：管理员 fanout（受众规则单一出处）+ 异步包装。

防回退要点：
- fanout 逐条投递、计数如实（部分失败不得报满）；
- 无管理员=``(0, 0)``（不是异常，由调用方决定是否告警）；
- 查询失败**向上抛**（不许伪装成「没有管理员」，否则库挂了反而静默）；
- 测试租户（``t-*``/``_t_*`` + ghost 清单 + env 追加）在出口拒写——QQ 旁路/
  库/流全不碰（T7-1，审计 H5）。
"""

from __future__ import annotations

import pytest

from backend.shared import notification_publisher as np


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *args, **kwargs):
        return _FakeResult(self._rows)


@pytest.mark.unit
def test_fanout_reaches_every_admin(monkeypatch):
    calls = []
    monkeypatch.setattr(
        np, "publish_notification", lambda **kw: calls.append(kw) or True
    )
    monkeypatch.setattr(
        "backend.shared.sync_db.sync_session",
        lambda: _FakeSession([("10000001", "default"), ("10000002", "tenant-b")]),
    )
    delivered, audience = np.publish_notification_to_admins(
        title="通达信桥账户通道掉线",
        content="连续 3 次探测失败",
        type="health",
        level="error",
    )
    assert (delivered, audience) == (2, 2)
    assert {c["user_id"] for c in calls} == {"10000001", "10000002"}
    assert all(c["type"] == "health" and c["level"] == "error" for c in calls)
    assert calls[1]["tenant_id"] == "tenant-b"


@pytest.mark.unit
def test_fanout_counts_partial_failure_honestly(monkeypatch):
    results = iter([True, False])
    monkeypatch.setattr(np, "publish_notification", lambda **kw: next(results))
    monkeypatch.setattr(
        "backend.shared.sync_db.sync_session",
        lambda: _FakeSession([("10000001", "default"), ("10000002", "default")]),
    )
    delivered, audience = np.publish_notification_to_admins(title="t", content="c")
    assert (delivered, audience) == (1, 2), "部分失败必须如实计数"


@pytest.mark.unit
def test_fanout_without_admins_is_zero_not_error(monkeypatch):
    monkeypatch.setattr(
        np, "publish_notification", lambda **kw: pytest.fail("无受众不得投递")
    )
    monkeypatch.setattr("backend.shared.sync_db.sync_session", lambda: _FakeSession([]))
    assert np.publish_notification_to_admins(title="t", content="c") == (0, 0)


@pytest.mark.unit
def test_fanout_query_failure_propagates(monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("backend.shared.sync_db.sync_session", _boom)
    with pytest.raises(RuntimeError):
        np.publish_notification_to_admins(title="t", content="c")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fanout_async_wrapper_delegates(monkeypatch):
    captured = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return (1, 1)

    monkeypatch.setattr(np, "publish_notification_to_admins", _fake)
    result = await np.publish_notification_to_admins_async(title="t", content="c")
    assert result == (1, 1) and captured["title"] == "t"


# ── QQ 旁路开关（2026-10-09 降噪）───────────────────────────────────


@pytest.mark.unit
def test_qq_alert_false_skips_per_event_bypass(monkeypatch):
    """``qq_alert=False`` 只关 QQ 旁路：站内写库照旧、逐条旁路不触发。

    批量告警生产者（市场情报/持仓预警）用它把 QQ 面让给摘要合并——
    2026-10-09 实测一段行情 26 条逐条推送把手机淹没。
    """
    alerts = []
    monkeypatch.setattr(np, "_maybe_qq_alert", lambda **kw: alerts.append(kw))
    monkeypatch.setattr(np, "get_db", None)  # 写库路径不参与本测：跳过即返回 False
    assert (
        np.publish_notification(
            user_id="1", tenant_id="default", title="t", content="c",
            level="warning", qq_alert=False,
        )
        is False
    )
    assert alerts == []


@pytest.mark.unit
def test_qq_alert_default_still_bypasses(monkeypatch):
    alerts = []
    monkeypatch.setattr(np, "_maybe_qq_alert", lambda **kw: alerts.append(kw))
    monkeypatch.setattr(np, "get_db", None)
    np.publish_notification(
        user_id="1", tenant_id="default", title="t", content="c", level="warning"
    )
    assert len(alerts) == 1


# ── 测试租户出口闸（T7-1，审计 H5）─────────────────────────────────


@pytest.mark.unit
def test_test_tenant_pushes_are_refused_before_qq_and_db(monkeypatch):
    """夹具租户的告警一律不出口：QQ 旁路不触发、库/流不写、返回 False。

    实测背景（2026-10-10）：集成测试的假 critical（t-* 夹具、"fake-model" 决策轮
    告警）真推 QQ + 落 notifications 表。推送出口唯一 = ``publish_notification``，
    在此一处拒写；夹具若要验发送行为应注入替身。
    """
    alerts = []
    counters = []
    monkeypatch.setattr(np, "_maybe_qq_alert", lambda **kw: alerts.append(kw))
    monkeypatch.setattr(np, "inc_counter", lambda _c, r: counters.append(r))
    monkeypatch.setattr(np, "get_db", None)  # 库路径若被走到也会在计数里现形
    for tid in ("t-pending-life-4e4040", "t-1abc", "t-doc-9x", "t-run-abc", "_t_p206"):
        assert (
            np.publish_notification(
                user_id="1", tenant_id=tid, title="t", content="c", level="error"
            )
            is False
        ), tid
    assert alerts == [], "测试租户连 QQ 旁路都不许走"
    assert counters == ["refused_test_tenant"] * 5, "拒写必须留 refused_test_tenant 计数"


@pytest.mark.unit
def test_real_tenants_still_pass_the_gate(monkeypatch):
    """拒收不能拒过头：default 与普通租户照常走旁路（近失配不得误伤）。"""
    alerts = []
    monkeypatch.setattr(np, "_maybe_qq_alert", lambda **kw: alerts.append(kw))
    monkeypatch.setattr(np, "get_db", None)  # 写库路径不参与：跳过即返回 False
    for tid in ("default", "team-alpha", "tenant-b", "test-lab"):
        np.publish_notification(
            user_id="1", tenant_id=tid, title="t", content="c", level="warning"
        )
    # get_db=None 时旁路先行、随后返回 False（与闸无关的既有语义）；只要旁路都触发了
    assert len(alerts) == 4
