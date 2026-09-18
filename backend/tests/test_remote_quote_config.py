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

_SCRIPT_RUNNER = (
    Path(__file__).resolve().parents[1]
    / "services"
    / "engine"
    / "inference"
    / "script_runner.py"
)

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


def test_empty_env_falls_back_to_free_feed(monkeypatch):
    """空串环境变量必须当作「未设置」处理（compose 是用 ${VAR:-} 传参的）。

    2026-09-18 事故：推理写库路径直读 `os.getenv("REMOTE_QUOTE_REDIS_PORT", "6379")`，
    而 os.getenv 的默认值只在变量「未设置」时生效 —— compose 传的空串会原样返回，
    `int("")` 抛 ValueError 把整个写库事务（含 DELETE）一起回滚，批次却仍记为
    completed + signals_count 有值，于是推理历史/排名榜永远是空的。
    """
    _clear(monkeypatch)
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_HOST", "")
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_PORT", "")
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_PASSWORD", "")
    monkeypatch.setenv("REMOTE_QUOTE_REDIS_DB", "")

    host, port, password, db = rqc.resolve_remote_quote_redis()

    assert (host, port, password, db) == (
        rqc.FREE_FEED_HOST,
        rqc.FREE_FEED_PORT,
        rqc.FREE_FEED_PASSWORD,
        rqc.FREE_FEED_DB,
    )
    assert isinstance(port, int)


def test_script_runner_uses_shared_quote_config():
    """推理写库路径取行情地址必须走共享配置，不得再直读 env（空串陷阱）。"""
    src = _SCRIPT_RUNNER.read_text(encoding="utf-8")

    assert "resolve_remote_quote_redis" in src
    assert 'os.getenv("REMOTE_QUOTE_REDIS_PORT"' not in src


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
