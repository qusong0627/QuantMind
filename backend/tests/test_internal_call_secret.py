"""内部调用密钥语义测试（C1 加固回归，2026-09-17）。

锁死三条：① runtime.env 权威（公开默认 env 不得遮蔽文件值——key 轮换曾因此失效）；
② 公开默认值（changeme-*/dev-*）一律视为未配置；③ 无有效密钥 = ""（一切内部校验必败）。
"""

from __future__ import annotations

import importlib

import pytest

from backend.shared import runtime_secrets as rs

_PUBLIC = "changeme-internal-secret"
_STRONG = "s" * 64


def _reload_auth(monkeypatch, tmp_path, file_value: str | None, env_value: str | None):
    """隔离 runtime.env（QM_RUNTIME_ENV_FILE）与环境变量后重新导入 auth 模块。"""
    env_file = tmp_path / "runtime.env"
    if file_value is not None:
        env_file.write_text(f"INTERNAL_CALL_SECRET={file_value}\n", encoding="utf-8")
    monkeypatch.setenv("QM_RUNTIME_ENV_FILE", str(env_file))
    if env_value is None:
        monkeypatch.delenv("INTERNAL_CALL_SECRET", raising=False)
    else:
        monkeypatch.setenv("INTERNAL_CALL_SECRET", env_value)
    from backend.shared import auth

    return importlib.reload(auth)


@pytest.mark.unit
def test_runtime_env_wins_over_public_default_env(monkeypatch, tmp_path):
    """文件值（轮换后的强密钥）优先于容器里烘着的公开默认值——轮换免重启的关键。"""
    auth = _reload_auth(monkeypatch, tmp_path, _STRONG, _PUBLIC)
    assert auth.get_internal_call_secret() == _STRONG


@pytest.mark.unit
def test_public_defaults_treated_as_unconfigured(monkeypatch, tmp_path):
    """无文件 + env 为公开默认 → 空（fail-closed，匿名者拿默认值不再能伪造）。"""
    auth = _reload_auth(monkeypatch, tmp_path, None, _PUBLIC)
    assert auth.get_internal_call_secret() == ""
    auth2 = _reload_auth(monkeypatch, tmp_path, None, "dev-internal-call-secret")
    assert auth2.get_internal_call_secret() == ""
    # 文件里只有公开默认值同样视为未配置
    auth3 = _reload_auth(monkeypatch, tmp_path, _PUBLIC, None)
    assert auth3.get_internal_call_secret() == ""


@pytest.mark.unit
def test_strong_env_used_when_no_file(monkeypatch, tmp_path):
    auth = _reload_auth(monkeypatch, tmp_path, None, _STRONG)
    assert auth.get_internal_call_secret() == _STRONG


@pytest.mark.unit
@pytest.mark.asyncio
async def test_verify_internal_call_rejects_when_unconfigured(monkeypatch, tmp_path):
    """无有效密钥时：不给头 / 给任何值 一律 401（含公开默认值）。"""
    auth = _reload_auth(monkeypatch, tmp_path, None, _PUBLIC)
    assert auth.get_internal_call_secret() == ""

    from fastapi import HTTPException

    from backend.services.trade.routers import internal_strategy_utils as util

    monkeypatch.setattr(util, "get_internal_call_secret", lambda: "")
    with pytest.raises(HTTPException) as e1:
        await util.verify_internal_call("changeme-internal-secret")
    assert e1.value.status_code == 401
    with pytest.raises(HTTPException) as e2:
        await util.verify_internal_call(None)
    assert e2.value.status_code == 401

    monkeypatch.setattr(util, "get_internal_call_secret", lambda: _STRONG)
    await util.verify_internal_call(_STRONG)  # 匹配 → 通过（不抛）
    with pytest.raises(HTTPException):
        await util.verify_internal_call("wrong")
