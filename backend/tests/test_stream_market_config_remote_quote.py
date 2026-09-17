"""回归：REMOTE_QUOTE_REDIS_PORT 空串容错。

背景（2026-09-17 实测事故）：compose 的「远端行情 Redis」块以空串默认注入
`REMOTE_QUOTE_REDIS_PORT`（语义：留空 = 用共享配置默认值），而 stream 服务的
`market_config.Settings` 将其声明为严格 int → 空串触发 pydantic int_parsing，
stream 启动即崩溃（看门狗 5 次后熔断，8003 不可达）。

修复：空串（或空白/None）回落到字段声明的默认端口 MARKET_REDIS_PORT。
"""
import os


def test_empty_remote_quote_redis_port_falls_back_to_default(monkeypatch):
    # Arrange：模拟 compose 注入的空串
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_PORT", "")
    from backend.services.stream.market_app import market_config as mc

    # Act
    settings = mc.Settings()

    # Assert：不抛 ValidationError，且落到默认端口
    assert settings.REMOTE_QUOTE_REDIS_PORT == mc.MARKET_REDIS_PORT


def test_whitespace_remote_quote_redis_port_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_PORT", "  ")
    from backend.services.stream.market_app import market_config as mc

    settings = mc.Settings()

    assert settings.REMOTE_QUOTE_REDIS_PORT == mc.MARKET_REDIS_PORT


def test_explicit_remote_quote_redis_port_is_honored(monkeypatch):
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_PORT", "6390")
    from backend.services.stream.market_app import market_config as mc

    settings = mc.Settings()

    assert settings.REMOTE_QUOTE_REDIS_PORT == 6390
