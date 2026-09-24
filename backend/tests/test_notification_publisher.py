"""通知发布器测试：管理员 fanout（受众规则单一出处）+ 异步包装。

防回退要点：
- fanout 逐条投递、计数如实（部分失败不得报满）；
- 无管理员=``(0, 0)``（不是异常，由调用方决定是否告警）；
- 查询失败**向上抛**（不许伪装成「没有管理员」，否则库挂了反而静默）。
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
