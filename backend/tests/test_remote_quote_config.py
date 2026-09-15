"""T-P0-03 回归：远端行情配置唯一源 + AI-IDE runner 敏感 env 白名单。

背景：
1) REMOTE_QUOTE_REDIS_* 的默认值（公共免费行情服地址/口令）此前在两个文件
   各写一份，现收敛到 backend/shared/remote_quote_config.py；
2) AI-IDE 用户代码容器此前透传 SECRET_KEY/JWT_SECRET_KEY/INTERNAL_CALL_SECRET/
   DASHSCOPE_API_KEY/QWEN_API_KEY，runner 不需要这些签钥与 LLM Key。
"""

from pathlib import Path

import pytest

from backend.shared import remote_quote_config as rqc

_ENV_KEYS = [
    "REMOTE_QUOTE_REDIS_HOST",
    "REMOTE_QUOTE_REDIS_PORT",
    "REMOTE_QUOTE_REDIS_PASSWORD",
    "REMOTE_QUOTE_REDIS_DB",
    "REMOTE_QUOTE_DISABLED",
]


def _clear(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(rqc, "_root_env_cache", {})  # 隔离真实项目根 .env


def test_default_is_free_feed(monkeypatch):
    _clear(monkeypatch)
    host, port, password, db = rqc.resolve_remote_quote_redis()
    assert host == rqc.FREE_FEED_HOST
    assert port == rqc.FREE_FEED_PORT
    assert password == rqc.FREE_FEED_PASSWORD
    assert db == rqc.FREE_FEED_DB


def test_env_override_wins(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_HOST", "quote.internal")
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_PORT", "6380")
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_PASSWORD", "s3cret")
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_DB", "5")
    assert rqc.resolve_remote_quote_redis() == ("quote.internal", 6380, "s3cret", 5)


def test_disabled_returns_none(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("REMOTE_QUOTE_DISABLED", "true")
    assert rqc.resolve_remote_quote_redis() is None


def test_root_env_fallback(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setattr(
        rqc, "_root_env_cache", {"REMOTE_QUOTE_REDIS_HOST": "from-root-env"}
    )
    assert rqc.resolve_remote_quote_redis()[0] == "from-root-env"


_EXECUTOR = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "engine"
    / "routers"
    / "ai_ide"
    / "executor.py"
)

_FORBIDDEN_ENV_KEYS = (
    "SECRET_KEY",
    "JWT_SECRET_KEY",
    "INTERNAL_CALL_SECRET",
    "DASHSCOPE_API_KEY",
    "QWEN_API_KEY",
)


def test_executor_passthrough_excludes_secrets():
    """passthrough_keys 列表中不得出现签钥/LLM Key（注释中提及不算）。"""
    src = _EXECUTOR.read_text(encoding="utf-8")
    start = src.index("passthrough_keys = [")
    end = src.index("]", start)
    block = src[start:end]
    for key in _FORBIDDEN_ENV_KEYS:
        assert f'"{key}"' not in block, f"executor passthrough 仍含敏感 key: {key}"
