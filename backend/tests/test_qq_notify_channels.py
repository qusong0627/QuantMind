"""分市场 QQ 通道测试（2026-10-08 用户裁决「港股/美股各一台官方机器人」）。

覆盖两侧实现（容器 `backend/shared/qq_notify.py` + 宿主 `scripts/push_notify.py`），
两处必须同口径，防回退要点：

- 市场通道三键未配齐 → **回退默认通道**且标题带 ``[港股]/[美股]`` 前缀——
  宁可落进旧聊天窗，不可静默丢消息；
- 三键齐备 → 走本通道：openid 取本通道、令牌缓存文件独立
  （``qqbot_token_hk.json``），绝不与默认通道互串；
- ``notify_async`` 不做等级过滤（委托回执/分析摘要用）；``alert_async``
  仍只发 warning/error，但通道参数必须透传；
- 宿主 CLI ``--channel`` 必须端到端可用（HK 循环调用入口）。

宿主模块加载说明：``push_notify.py`` 在 import 时会 ``_load_env()`` 把
keeper.env 的真实凭据 ``setdefault`` 进进程环境——fixture 负责立刻回滚，
避免污染同进程的其他测试。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from backend.shared import qq_notify

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_DEFAULT_KEYS = {
    "QQ_BOT_APP_ID": "d-id",
    "QQ_BOT_APP_SECRET": "d-sec",
    "QQ_BOT_OWNER_OPENID": "d-oid",
}
_HK_KEYS = {
    "QQ_BOT_HK_APP_ID": "h-id",
    "QQ_BOT_HK_APP_SECRET": "h-sec",
    "QQ_BOT_HK_OWNER_OPENID": "h-oid",
}


def _secret(monkeypatch, values: dict) -> None:
    monkeypatch.setattr(
        qq_notify, "get_secret", lambda key, default="": values.get(key, default)
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


# ── 通道解析与回退 ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_resolve_market_channel_native_when_fully_configured(monkeypatch):
    _secret(monkeypatch, {**_DEFAULT_KEYS, **_HK_KEYS})
    keys, effective = qq_notify._resolve_channel("hk")
    assert effective == "hk"
    assert keys == qq_notify._MARKET_CHANNELS["hk"]


@pytest.mark.unit
def test_resolve_market_channel_falls_back_when_any_key_missing(monkeypatch):
    for missing in ("QQ_BOT_HK_APP_ID", "QQ_BOT_HK_APP_SECRET", "QQ_BOT_HK_OWNER_OPENID"):
        values = {**_DEFAULT_KEYS, **{k: v for k, v in _HK_KEYS.items() if k != missing}}
        _secret(monkeypatch, values)
        keys, effective = qq_notify._resolve_channel("hk")
        assert (keys, effective) == (qq_notify._DEFAULT_KEYS, "default"), missing


@pytest.mark.unit
def test_is_configured_market_falls_back_to_default(monkeypatch):
    """市场通道缺三键但默认通道在 → 仍算可发（回退路径），不许谎报不可用。"""
    _secret(monkeypatch, _DEFAULT_KEYS)
    assert qq_notify.is_configured("hk") is True


@pytest.mark.unit
def test_notify_fallback_prefixes_market_tag(monkeypatch):
    _secret(monkeypatch, _DEFAULT_KEYS)
    sent = {}
    monkeypatch.setattr(
        qq_notify,
        "send_markdown",
        lambda content, channel="default": sent.update(content=content, channel=channel)
        or {"id": "1"},
    )
    assert qq_notify.notify("买入腾讯", "正文", channel="hk") is True
    assert sent["channel"] == "default"
    assert sent["content"].startswith("[港股] 买入腾讯")


@pytest.mark.unit
def test_notify_native_market_channel_has_no_tag(monkeypatch):
    _secret(monkeypatch, {**_DEFAULT_KEYS, **_HK_KEYS})
    sent = {}
    monkeypatch.setattr(
        qq_notify,
        "send_markdown",
        lambda content, channel="default": sent.update(content=content, channel=channel)
        or {"id": "1"},
    )
    assert qq_notify.notify("买入腾讯", channel="hk") is True
    assert sent["channel"] == "hk"
    assert not sent["content"].startswith("[港股]"), "原生通道不得再加前缀"


# ── 令牌缓存按通道隔离 ───────────────────────────────────────────────────


@pytest.mark.unit
def test_token_cache_path_per_channel(monkeypatch, tmp_path):
    monkeypatch.setenv("QM_QQ_TOKEN_CACHE", str(tmp_path / "qqbot_token.json"))
    assert qq_notify._token_cache_path() == tmp_path / "qqbot_token.json"
    assert qq_notify._token_cache_path("hk") == tmp_path / "qqbot_token_hk.json"
    assert qq_notify._token_cache_path("us") == tmp_path / "qqbot_token_us.json"


@pytest.mark.unit
def test_get_token_hk_uses_hk_credentials_and_cache(monkeypatch, tmp_path):
    _secret(monkeypatch, {**_DEFAULT_KEYS, **_HK_KEYS})
    monkeypatch.setenv("QM_QQ_TOKEN_CACHE", str(tmp_path / "qqbot_token.json"))
    calls = []

    def _fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResp({"access_token": "tok-hk", "expires_in": 7200})

    monkeypatch.setattr("requests.post", _fake_post)
    assert qq_notify._get_token("hk") == "tok-hk"
    assert calls[0][1]["json"] == {"appId": "h-id", "clientSecret": "h-sec"}
    assert (tmp_path / "qqbot_token_hk.json").exists()
    assert not (tmp_path / "qqbot_token.json").exists(), "HK 令牌不得写进默认缓存"


@pytest.mark.unit
def test_get_token_partial_hk_falls_back_to_default_cache(monkeypatch, tmp_path):
    """hk 缺键时取的是**默认机器人**的令牌——缓存也必须落默认文件。"""
    _secret(monkeypatch, _DEFAULT_KEYS)
    monkeypatch.setenv("QM_QQ_TOKEN_CACHE", str(tmp_path / "qqbot_token.json"))
    calls = []

    def _fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResp({"access_token": "tok-d", "expires_in": 7200})

    monkeypatch.setattr("requests.post", _fake_post)
    assert qq_notify._get_token("hk") == "tok-d"
    assert calls[0][1]["json"] == {"appId": "d-id", "clientSecret": "d-sec"}
    assert (tmp_path / "qqbot_token.json").exists()
    assert not (tmp_path / "qqbot_token_hk.json").exists()


@pytest.mark.unit
def test_send_text_hk_uses_hk_owner_openid(monkeypatch, tmp_path):
    _secret(monkeypatch, {**_DEFAULT_KEYS, **_HK_KEYS})
    monkeypatch.setenv("QM_QQ_TOKEN_CACHE", str(tmp_path / "qqbot_token.json"))
    captured = {}

    def _fake_post(url, **kwargs):
        if url == qq_notify.TOKEN_URL:
            return _FakeResp({"access_token": "tok", "expires_in": 7200})
        captured["url"] = url
        captured["auth"] = kwargs["headers"]["Authorization"]
        return _FakeResp({"id": "m1"})

    monkeypatch.setattr("requests.post", _fake_post)
    qq_notify.send_text("hi", channel="hk")
    assert captured["url"].endswith("/v2/users/h-oid/messages")
    assert captured["auth"] == "QQBot tok"


# ── 旁路（notify_async / alert_async）的通道透传 ─────────────────────────


@pytest.mark.unit
def test_notify_async_passes_channel_without_level_filter(monkeypatch):
    """委托回执/分析摘要走 notify_async：无等级过滤 + 通道透传。"""
    from queue import Queue

    q: Queue = Queue()
    monkeypatch.setattr(
        qq_notify,
        "notify",
        lambda title, content="", channel="default": q.put((title, content, channel))
        or True,
    )
    qq_notify.notify_async("✅ 富途下单", "委托号：O1", channel="hk")
    title, content, channel = q.get(timeout=3)
    assert (title, content, channel) == ("✅ 富途下单", "委托号：O1", "hk")


@pytest.mark.unit
def test_alert_async_passes_channel(monkeypatch):
    from queue import Queue

    _secret(monkeypatch, _DEFAULT_KEYS)
    q: Queue = Queue()
    monkeypatch.setattr(
        qq_notify,
        "notify",
        lambda title, content="", channel="default": q.put((title, content, channel))
        or True,
    )
    assert (
        qq_notify.alert_async(
            level="warning",
            title="富途下单失败",
            content="opend down",
            alert_type="futu-bridge",
            channel="us",
        )
        is True
    )
    title, _, channel = q.get(timeout=3)
    assert title == "[futu-bridge] 富途下单失败"
    assert channel == "us"


# ── 宿主侧 scripts/push_notify.py（HK 循环的发送入口） ───────────────────


@pytest.fixture()
def push_mod(monkeypatch, tmp_path):
    """按路径加载宿主 push_notify；import 期间进环境的真实凭据立即回滚。"""
    spec = importlib.util.spec_from_file_location(
        "push_notify_host", _PROJECT_ROOT / "scripts" / "push_notify.py"
    )
    mod = importlib.util.module_from_spec(spec)
    before = dict(os.environ)
    try:
        spec.loader.exec_module(mod)
    finally:
        for key in list(os.environ):
            if key not in before:
                del os.environ[key]
        os.environ.update(before)
    monkeypatch.setattr(mod, "_log", lambda msg: None)
    return mod


@pytest.mark.unit
def test_host_fallback_prefixes_hk_tag(push_mod, monkeypatch):
    for key in push_mod._CHANNEL_KEYS["hk"]:
        monkeypatch.delenv(key, raising=False)
    sent = {}
    monkeypatch.setattr(
        push_mod,
        "send_markdown",
        lambda content, channel="default": sent.update(content=content, channel=channel)
        or {"id": "1"},
    )
    push_mod.notify("买入 00700.HK 100股", "正文", channel="hk")
    assert sent["channel"] == "default"
    assert sent["content"].startswith("[港股] 买入 00700.HK 100股")


@pytest.mark.unit
def test_host_native_hk_channel_no_tag(push_mod, monkeypatch):
    monkeypatch.setenv("QQ_BOT_HK_APP_ID", "x")
    monkeypatch.setenv("QQ_BOT_HK_APP_SECRET", "y")
    monkeypatch.setenv("QQ_BOT_HK_OWNER_OPENID", "z")
    sent = {}
    monkeypatch.setattr(
        push_mod,
        "send_markdown",
        lambda content, channel="default": sent.update(content=content, channel=channel)
        or {"id": "1"},
    )
    push_mod.notify("买入 00700.HK", "", channel="hk")
    assert sent["channel"] == "hk"
    assert not sent["content"].startswith("[港股]")


@pytest.mark.unit
def test_host_token_cache_names(push_mod, monkeypatch, tmp_path):
    monkeypatch.setattr(push_mod, "TOKEN_CACHE", tmp_path / "qqbot_token.json")
    assert push_mod._token_cache("default") == tmp_path / "qqbot_token.json"
    assert push_mod._token_cache("hk") == tmp_path / "qqbot_token_hk.json"
    assert push_mod._token_cache("us") == tmp_path / "qqbot_token_us.json"


@pytest.mark.unit
def test_host_get_token_hk_uses_hk_secret(push_mod, monkeypatch, tmp_path):
    monkeypatch.setenv("QQ_BOT_HK_APP_ID", "h-id")
    monkeypatch.setenv("QQ_BOT_HK_APP_SECRET", "h-sec")
    monkeypatch.setattr(push_mod, "TOKEN_CACHE", tmp_path / "qqbot_token.json")
    calls = []

    def _fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FakeResp({"access_token": "tok-hk", "expires_in": 7200})

    monkeypatch.setattr("requests.post", _fake_post)
    assert push_mod._get_token("hk") == "tok-hk"
    assert calls[0][1]["json"] == {"appId": "h-id", "clientSecret": "h-sec"}
    assert (tmp_path / "qqbot_token_hk.json").exists()


@pytest.mark.unit
def test_host_send_rejects_missing_market_openid(push_mod, monkeypatch):
    """原生 hk 通道缺 owner openid 时不许静默发——回退已在 resolve 层发生，
    走到 send 层的 openid 必是完整的；这里钉死缺 openid 的报错路径。"""
    monkeypatch.delenv("QQ_BOT_HK_OWNER_OPENID", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        push_mod.send_text("hi", channel="hk")
    assert "QQ_BOT_HK_OWNER_OPENID" in str(excinfo.value)


@pytest.mark.unit
def test_host_cli_channel_flag_end_to_end(push_mod, monkeypatch):
    import sys

    captured = {}
    monkeypatch.setattr(
        push_mod,
        "notify",
        lambda title, content, channel="default": captured.update(
            title=title, content=content, channel=channel
        ),
    )
    monkeypatch.setattr(
        sys, "argv", ["push_notify.py", "send", "标题", "正文", "--channel", "hk"]
    )
    assert push_mod.main() == 0
    assert captured == {"title": "标题", "content": "正文", "channel": "hk"}


@pytest.mark.unit
def test_host_cli_default_channel_without_flag(push_mod, monkeypatch):
    import sys

    captured = {}
    monkeypatch.setattr(
        push_mod,
        "notify",
        lambda title, content, channel="default": captured.update(channel=channel),
    )
    monkeypatch.setattr(sys, "argv", ["push_notify.py", "send", "标题", "正文"])
    assert push_mod.main() == 0
    assert captured["channel"] == "default"
