"""QQ 推送通道测试：配置三键 / 令牌缓存 / 发送契约 / 降级 / 告警旁路 / 接线断言。

防回退要点：
- 未配置时 notify 必须**如实 False**（不假装发出）；
- 令牌缓存命中不得再打 HTTP；
- Authorization 必须是 ``QQBot <token>``（Bearer 会被腾讯拒 11241）；
- 告警旁路（alert_async）只发 warning/error 且不得阻塞调用方；
- notification_publisher 的 QQ 旁路必须**先于**库面检查（库写失败告警也要到人）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.shared import qq_notify

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_KEYS = {
    "QQ_BOT_APP_ID": "test-app-id",
    "QQ_BOT_APP_SECRET": "test-secret",
    "QQ_BOT_OWNER_OPENID": "test-openid",
}


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(
        qq_notify, "get_secret", lambda key, default="": _KEYS.get(key, default)
    )


class _FakeResp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


# ── 配置判定 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_is_configured_requires_all_three(monkeypatch):
    for missing in ("QQ_BOT_APP_ID", "QQ_BOT_APP_SECRET", "QQ_BOT_OWNER_OPENID"):
        values = {k: v for k, v in _KEYS.items() if k != missing}
        monkeypatch.setattr(
            qq_notify,
            "get_secret",
            lambda key, default="", _values=values: _values.get(key, default),
        )
        assert qq_notify.is_configured() is False, missing
    monkeypatch.setattr(
        qq_notify, "get_secret", lambda key, default="": _KEYS.get(key, default)
    )
    assert qq_notify.is_configured() is True


@pytest.mark.unit
def test_notify_unconfigured_returns_false_without_sending(monkeypatch):
    monkeypatch.setattr(qq_notify, "get_secret", lambda key, default="": "")
    monkeypatch.setattr(
        qq_notify, "send_markdown", lambda *a, **k: pytest.fail("未配置不得发送")
    )
    assert qq_notify.notify("t", "c") is False


# ── 令牌缓存 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_token_cache_hit_skips_http(configured, monkeypatch, tmp_path):
    cache = tmp_path / "token.json"
    cache.write_text(
        json.dumps({"token": "cached-token", "expires_at": time.time() + 3600}),
        encoding="utf-8",
    )
    monkeypatch.setenv("QM_QQ_TOKEN_CACHE", str(cache))
    monkeypatch.setattr(
        "requests.post", lambda *a, **k: pytest.fail("缓存命中不得打 HTTP")
    )
    assert qq_notify._get_token() == "cached-token"


@pytest.mark.unit
def test_token_expired_refetches_and_writes_cache(configured, monkeypatch, tmp_path):
    cache = tmp_path / "token.json"
    cache.write_text(
        json.dumps({"token": "stale", "expires_at": time.time() - 10}), encoding="utf-8"
    )
    monkeypatch.setenv("QM_QQ_TOKEN_CACHE", str(cache))
    calls = []

    def _fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResp({"access_token": "fresh-token", "expires_in": 7200})

    monkeypatch.setattr("requests.post", _fake_post)
    assert qq_notify._get_token() == "fresh-token"
    assert calls[0][0] == qq_notify.TOKEN_URL
    assert calls[0][1]["json"] == {
        "appId": "test-app-id",
        "clientSecret": "test-secret",
    }
    assert json.loads(cache.read_text(encoding="utf-8"))["token"] == "fresh-token"


# ── 发送契约 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_send_text_uses_qqbot_auth_and_v2_endpoint(configured, monkeypatch, tmp_path):
    monkeypatch.setenv("QM_QQ_TOKEN_CACHE", str(tmp_path / "t.json"))
    captured = {}

    def _fake_post(url, **kwargs):
        if url == qq_notify.TOKEN_URL:
            return _FakeResp({"access_token": "tok", "expires_in": 7200})
        captured["url"] = url
        captured["headers"] = kwargs["headers"]
        captured["json"] = kwargs["json"]
        return _FakeResp({"id": "msg-1"})

    monkeypatch.setattr("requests.post", _fake_post)
    out = qq_notify.send_text("hello")
    assert out == {"id": "msg-1"}
    assert captured["url"] == "https://api.sgroup.qq.com/v2/users/test-openid/messages"
    assert captured["headers"]["Authorization"] == "QQBot tok"
    assert captured["json"]["msg_type"] == 0 and captured["json"]["content"] == "hello"


@pytest.mark.unit
def test_business_error_code_in_a_200_body_is_a_failure(
    configured, monkeypatch, tmp_path
):
    """配额/频控这类失败可以是 **HTTP 200 + body 里的 code≠0**。

    原先发送路径只 ``raise_for_status()`` 就把 body 丢了：平台拒了、日志记「已推送」，
    手机上什么都没收到——本仓最贵的就是这种静默失败。报错须带 code 与 message，
    且不得回显令牌。
    """
    monkeypatch.setenv("QM_QQ_TOKEN_CACHE", str(tmp_path / "t.json"))

    def _fake_post(url, **kwargs):
        if url == qq_notify.TOKEN_URL:
            return _FakeResp({"access_token": "tok-secret", "expires_in": 7200})
        return _FakeResp({"code": 11244, "message": "主动消息额度不足"})

    monkeypatch.setattr("requests.post", _fake_post)
    with pytest.raises(RuntimeError) as excinfo:
        qq_notify.send_text("hello")
    text = str(excinfo.value)
    assert "11244" in text and "额度" in text
    assert "tok-secret" not in text, "报错里不得回显令牌"


@pytest.mark.unit
def test_success_body_without_code_is_not_treated_as_error(
    configured, monkeypatch, tmp_path
):
    """成功体只有 id/timestamp（没有 code 字段）——不许把正常返回判成失败。"""
    monkeypatch.setenv("QM_QQ_TOKEN_CACHE", str(tmp_path / "t.json"))

    def _fake_post(url, **kwargs):
        if url == qq_notify.TOKEN_URL:
            return _FakeResp({"access_token": "tok", "expires_in": 7200})
        return _FakeResp({"id": "msg-9", "timestamp": 1})

    monkeypatch.setattr("requests.post", _fake_post)
    assert qq_notify.send_markdown("**x**") == {"id": "msg-9", "timestamp": 1}


# ── notify 降级链 ────────────────────────────────────────────────────────


@pytest.mark.unit
def test_notify_falls_back_to_text_when_markdown_rejected(configured, monkeypatch):
    sent = {}

    def _boom(*a, **k):
        raise RuntimeError("markdown not allowed")

    def _text(content):
        sent["text"] = content
        return {"id": "1"}

    monkeypatch.setattr(qq_notify, "send_markdown", _boom)
    monkeypatch.setattr(qq_notify, "send_text", _text)
    assert qq_notify.notify("标题", "**加粗**正文") is True
    assert sent["text"] == "标题\n\n加粗正文", "降级必须剥掉 markdown 星号"


@pytest.mark.unit
def test_notify_both_paths_failed_returns_false(configured, monkeypatch):
    monkeypatch.setattr(
        qq_notify,
        "send_markdown",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")),
    )
    monkeypatch.setattr(
        qq_notify, "send_text", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("y"))
    )
    assert qq_notify.notify("t") is False


# ── 告警旁路 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_alert_async_filters_info_level(configured, monkeypatch):
    monkeypatch.setattr(
        qq_notify, "notify", lambda *a, **k: pytest.fail("info 不得外发")
    )
    assert qq_notify.alert_async(level="info", title="t") is False
    assert qq_notify.alert_async(level="success", title="t") is False


@pytest.mark.unit
def test_alert_async_sends_warning_via_thread(configured, monkeypatch):
    from queue import Queue

    q: Queue = Queue()
    monkeypatch.setattr(
        qq_notify, "notify", lambda title, content="": q.put((title, content)) or True
    )
    assert (
        qq_notify.alert_async(
            level="warning", title="桥掉线", content="8550 不通", alert_type="health"
        )
        is True
    )
    title, content = q.get(timeout=3)
    assert title == "[health] 桥掉线" and content == "8550 不通"


@pytest.mark.unit
def test_alert_async_unconfigured_is_false(monkeypatch):
    monkeypatch.setattr(qq_notify, "get_secret", lambda key, default="": "")
    assert qq_notify.alert_async(level="error", title="t") is False


@pytest.mark.unit
def test_alert_async_force_bypasses_level_filter(configured, monkeypatch):
    """告警恢复类事件（level=success）经 force 显式放行——掉线闭环要回手机。"""
    from queue import Queue

    q: Queue = Queue()
    monkeypatch.setattr(
        qq_notify, "notify", lambda title, content="": q.put((title, content)) or True
    )
    assert (
        qq_notify.alert_async(
            level="success", title="通达信桥已恢复", alert_type="health", force=True
        )
        is True
    )
    title, _ = q.get(timeout=3)
    assert title == "[health] 通达信桥已恢复"


@pytest.mark.unit
def test_alert_async_force_still_requires_config(monkeypatch):
    monkeypatch.setattr(qq_notify, "get_secret", lambda key, default="": "")
    assert qq_notify.alert_async(level="success", title="t", force=True) is False


# ── 接线断言 ─────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_publisher_bypass_runs_before_db_guard(monkeypatch):
    """库面失败（FK 缺用户/db 池不可用）时告警仍要出门——旁路必须先于库检查。"""
    from backend.shared import notification_publisher as np

    calls = []
    monkeypatch.setattr(
        qq_notify,
        "alert_async",
        lambda **kw: calls.append(kw) or True,
    )
    monkeypatch.setattr(np, "get_db", None)
    result = np.publish_notification(
        user_id="00000001",
        tenant_id="default",
        title="风控告警",
        content="L3 触发",
        type="sentinel",
        level="warning",
    )
    assert result is False, "db 池不可用应返回 False"
    assert calls and calls[0]["level"] == "warning" and calls[0]["title"] == "风控告警"
    assert calls[0]["alert_type"] == "sentinel"


@pytest.mark.unit
def test_publisher_source_keeps_bypass_call():
    src = (_PROJECT_ROOT / "backend/shared/notification_publisher.py").read_text(
        encoding="utf-8"
    )
    body = src.split("def publish_notification(", 1)[1]
    bypass_pos = body.find("_maybe_qq_alert(")
    guard_pos = body.find("if get_db is None")
    assert bypass_pos != -1, "QQ 告警旁路被删"
    assert 0 <= bypass_pos < guard_pos, "旁路必须早于库面检查"
