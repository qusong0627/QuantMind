"""QuantDB SDK 客户端工厂（B1）测试。

契约：所有调用点共用同一套 Key 读取与超时/重试；同步比在线查询更宽；
Key 缺失抛 RuntimeError；QUANTDB_API_HOST 可覆盖。
"""

from __future__ import annotations

import pytest

pytest.importorskip("quantdb_sdk")


def _fake_sdk(monkeypatch):
    import quantdb_sdk

    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(quantdb_sdk, "QuantDBClient", FakeClient)
    return captured


def test_sync_purpose_uses_wide_timeout(monkeypatch):
    captured = _fake_sdk(monkeypatch)
    monkeypatch.setattr(
        "backend.shared.runtime_secrets.get_secret", lambda key: "k-test"
    )
    monkeypatch.delenv("QUANTDB_API_HOST", raising=False)

    from backend.shared.quantdb_client import get_client

    get_client("sync")

    assert captured["api_key"] == "k-test"
    assert captured["timeout"] == (15, 300)
    assert captured["max_retries"] == 3
    assert "api_host" not in captured


def test_adapter_purpose_uses_short_timeout(monkeypatch):
    captured = _fake_sdk(monkeypatch)
    monkeypatch.setattr(
        "backend.shared.runtime_secrets.get_secret", lambda key: "k-test"
    )

    from backend.shared.quantdb_client import get_client

    get_client("adapter")

    assert captured["timeout"] == (15, 120)
    assert captured["max_retries"] == 3


def test_missing_key_raises(monkeypatch):
    _fake_sdk(monkeypatch)
    monkeypatch.setattr("backend.shared.runtime_secrets.get_secret", lambda key: None)

    from backend.shared.quantdb_client import get_client

    with pytest.raises(RuntimeError, match="QUANTDB_API_KEY"):
        get_client("sync")


def test_api_host_override(monkeypatch):
    captured = _fake_sdk(monkeypatch)
    monkeypatch.setattr(
        "backend.shared.runtime_secrets.get_secret", lambda key: "k-test"
    )
    monkeypatch.setenv("QUANTDB_API_HOST", "https://example.test")

    from backend.shared.quantdb_client import get_client

    get_client("admin")

    assert captured["api_host"] == "https://example.test"
